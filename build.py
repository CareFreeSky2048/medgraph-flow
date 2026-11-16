"""Build offline single-file HTML and deployable PWA bundle for MedGraph Flow."""
import json
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
WEB_DIR = APP_DIR / "web"
DIST_DIR = APP_DIR / "dist"
PWA_DIR = DIST_DIR / "pwa"

from schema import validate_graph  # noqa: E402


def safe_json(value: dict) -> str:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def build() -> None:
    graph_path = DATA_DIR / "graph.json"
    if not graph_path.exists():
        raise FileNotFoundError(f"Missing {graph_path}; run seed_builder.py first.")

    graph = validate_graph(json.loads(graph_path.read_text(encoding="utf-8")))
    for node in graph["nodes"]:
        for da in node.get("diagram_assets", []) or []:
            rel = str(da.get("path", "")).replace("\\", "/")
            if not (DATA_DIR / rel).exists():
                raise FileNotFoundError(f"Missing referenced diagram asset for {node['id']}: {DATA_DIR / rel}")
    shell = (WEB_DIR / "shell.html").read_text(encoding="utf-8")
    css = (WEB_DIR / "style.css").read_text(encoding="utf-8")
    router_js = (WEB_DIR / "router.js").read_text(encoding="utf-8")
    sync_js = (WEB_DIR / "sync.js").read_text(encoding="utf-8")
    app_js = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    DIST_DIR.mkdir(parents=True, exist_ok=True)
    PWA_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Self-contained offline single HTML (works directly via file:// on PC/mobile)
    offline_scripts = (
        f'<script id="medgraph-data" type="application/json">{safe_json(graph)}</script>\n'
        f"<script>\n{router_js}\n{sync_js}\n{app_js}\n</script>"
    )
    offline_html = (
        shell.replace("__PWA_HEAD__", "")
        .replace("__STYLE__", css)
        .replace("__SCRIPTS__", offline_scripts)
    )
    (DIST_DIR / "medgraph_offline.html").write_text(offline_html, encoding="utf-8")

    # 2. Deployable PWA bundle (loads ./graph.json network-first so slide updates roll out cleanly)
    pwa_head = '<link rel="manifest" href="./manifest.json">'
    pwa_scripts = (
        f'<script id="medgraph-data" type="application/json">{safe_json(graph)}</script>\n'
        f"<script>\n{router_js}\n{sync_js}\n{app_js}\n</script>"
    )
    pwa_html = (
        shell.replace("__PWA_HEAD__", pwa_head)
        .replace("__STYLE__", css)
        .replace("__SCRIPTS__", pwa_scripts)
    )
    (PWA_DIR / "index.html").write_text(pwa_html, encoding="utf-8")
    shutil.copy2(graph_path, PWA_DIR / "graph.json")
    shutil.copy2(WEB_DIR / "manifest.json", PWA_DIR / "manifest.json")
    shutil.copy2(WEB_DIR / "sw.js", PWA_DIR / "sw.js")
    (PWA_DIR / ".nojekyll").write_text("", encoding="utf-8")

    # Copy preserved slide mechanism/anatomy/histology diagram assets to both dist/assets and dist/pwa/assets
    assets_src = DATA_DIR / "assets"
    asset_count = 0
    for target_assets in (DIST_DIR / "assets", PWA_DIR / "assets"):
        if target_assets.exists():
            shutil.rmtree(target_assets)
        if assets_src.exists():
            shutil.copytree(assets_src, target_assets)
    if assets_src.exists():
        asset_count = sum(1 for p in assets_src.rglob("*") if p.is_file())

    print(
        f"Build OK -> {DIST_DIR / 'medgraph_offline.html'} & {PWA_DIR} "
        f"({len(graph['nodes'])} nodes, {len(graph['edges'])} edges, {len(graph['threads'])} threads, {asset_count} assets)"
    )


if __name__ == "__main__":
    build()
