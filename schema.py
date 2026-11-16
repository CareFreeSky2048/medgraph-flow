"""JSON schema validation for MedGraph Flow 4-layer knowledge graph and progress."""
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

SUBJECTS = {
    "anatomy",
    "histology",
    "embryology",
    "physiology",
    "biochem",
    "pathophys",
    "pathology",
    "pharmacology",
}

SYLLABUS_TIERS = {
    "core_underlined": 3.5,
    "master": 3.0,
    "familiar": 2.0,
    "understand": 1.0,
}

NODE_TYPES = {
    "mechanism_flow",
    "structure_cloze",
    "comparison_matrix",
}

RELATION_TYPES = {
    "causal_next",
    "prerequisite",
    "structural_basis",
    "clinical_manifestation",
    "contrast_with",
    "cross_discipline",
}


def validate_node_payload(node_type: str, payload: dict, node_id: str) -> None:
    if not isinstance(payload, dict):
        raise ValueError(f"{node_id}: payload must be a dict")
    if node_type == "mechanism_flow":
        steps = payload.get("steps")
        if not isinstance(steps, list) or len(steps) < 2:
            raise ValueError(f"{node_id}: mechanism_flow requires >= 2 steps")
        for idx, step in enumerate(steps):
            if not isinstance(step, dict) or not step.get("cue") or not step.get("reveal"):
                raise ValueError(f"{node_id}: step {idx} missing cue/reveal")
        if not payload.get("takeaway"):
            raise ValueError(f"{node_id}: mechanism_flow missing takeaway")
    elif node_type == "structure_cloze":
        bullets = payload.get("bullets")
        if not isinstance(bullets, list) or len(bullets) < 1:
            raise ValueError(f"{node_id}: structure_cloze requires >= 1 bullet")
        if not any("{{" in str(b) and "}}" in str(b) for b in bullets):
            raise ValueError(f"{node_id}: structure_cloze needs at least one {{{{cloze}}}} capsule")
    elif node_type == "comparison_matrix":
        cols = payload.get("columns")
        rows = payload.get("rows")
        quiz = payload.get("quiz")
        if not isinstance(cols, list) or len(cols) < 3:
            raise ValueError(f"{node_id}: comparison_matrix needs >= 3 columns")
        if not isinstance(rows, list) or len(rows) < 2:
            raise ValueError(f"{node_id}: comparison_matrix needs >= 2 rows")
        for r_idx, row in enumerate(rows):
            if not isinstance(row, list) or len(row) != len(cols):
                raise ValueError(f"{node_id}: row {r_idx} column count mismatch")
        if not isinstance(quiz, dict) or not quiz.get("stem") or not isinstance(quiz.get("options"), list):
            raise ValueError(f"{node_id}: comparison_matrix missing valid quiz")
        ans = quiz.get("answer_idx")
        if not isinstance(ans, int) or ans < 0 or ans >= len(quiz["options"]):
            raise ValueError(f"{node_id}: quiz answer_idx out of range")


def validate_graph(graph: dict) -> dict:
    if not isinstance(graph, dict):
        raise ValueError("Graph root must be an object")
    if graph.get("schema_version") != 1:
        raise ValueError("Unsupported graph schema_version (expected 1)")

    syllabus = graph.get("syllabus", [])
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    threads = graph.get("threads", [])

    if not isinstance(syllabus, list) or not isinstance(nodes, list) or not isinstance(edges, list) or not isinstance(threads, list):
        raise ValueError("syllabus, nodes, edges, threads must be lists")

    syl_ids = set()
    for item in syllabus:
        sid = item.get("id")
        if not sid or sid in syl_ids:
            raise ValueError(f"Invalid or duplicate syllabus id: {sid}")
        if item.get("subject") not in SUBJECTS:
            raise ValueError(f"Invalid syllabus subject in {sid}: {item.get('subject')}")
        if item.get("tier") not in SYLLABUS_TIERS:
            raise ValueError(f"Invalid syllabus tier in {sid}: {item.get('tier')}")
        syl_ids.add(sid)

    node_ids = set()
    for node in nodes:
        nid = node.get("id")
        if not nid or nid in node_ids:
            raise ValueError(f"Invalid or duplicate node id: {nid}")
        if node.get("subject") not in SUBJECTS:
            raise ValueError(f"Invalid node subject in {nid}: {node.get('subject')}")
        if node.get("syllabus_tier") not in SYLLABUS_TIERS:
            raise ValueError(f"Invalid node tier in {nid}: {node.get('syllabus_tier')}")
        for sref in node.get("syllabus_refs", []):
            if syl_ids and sref not in syl_ids:
                raise ValueError(f"Node {nid} references unknown syllabus id: {sref}")
        ntype = node.get("node_type")
        if ntype not in NODE_TYPES:
            raise ValueError(f"Invalid node_type in {nid}: {ntype}")
        if not str(node.get("anchor_summary", "")).strip():
            raise ValueError(f"Node {nid} missing anchor_summary")
        validate_node_payload(ntype, node.get("payload"), nid)
        node_ids.add(nid)

    edge_ids = set()
    edge_map = {}
    for edge in edges:
        eid = edge.get("id")
        if not eid or eid in edge_ids:
            raise ValueError(f"Invalid or duplicate edge id: {eid}")
        src, dst = edge.get("source"), edge.get("target")
        if src not in node_ids or dst not in node_ids:
            raise ValueError(f"Edge {eid} has dangling endpoint: {src} -> {dst}")
        if src == dst:
            raise ValueError(f"Edge {eid} cannot be a self-loop: {src}")
        if edge.get("relation_type") not in RELATION_TYPES:
            raise ValueError(f"Edge {eid} invalid relation_type: {edge.get('relation_type')}")
        if not str(edge.get("bridge_text", "")).strip():
            raise ValueError(f"Edge {eid} missing bridge_text (logic must be explicit)")
        strength = edge.get("strength", 0.8)
        if not isinstance(strength, (int, float)) or not (0.0 < float(strength) <= 1.0):
            raise ValueError(f"Edge {eid} invalid strength: {strength}")
        edge_ids.add(eid)
        edge_map[eid] = edge

    thread_ids = set()
    for th in threads:
        tid = th.get("id")
        if not tid or tid in thread_ids:
            raise ValueError(f"Invalid or duplicate thread id: {tid}")
        steps = th.get("ordered_steps", [])
        if not isinstance(steps, list) or len(steps) < 2:
            raise ValueError(f"Thread {tid} must have >= 2 steps")
        for idx, step in enumerate(steps):
            nid = step.get("node_id")
            if nid not in node_ids:
                raise ValueError(f"Thread {tid} step {idx} references unknown node: {nid}")
            inc = step.get("incoming_edge_id")
            if idx > 0 and inc is not None:
                if inc not in edge_ids:
                    raise ValueError(f"Thread {tid} step {idx} references unknown edge: {inc}")
                e = edge_map[inc]
                prev_nid = steps[idx - 1]["node_id"]
                if not ((e["source"] == prev_nid and e["target"] == nid) or (e["relation_type"] == "contrast_with" and e["target"] == prev_nid and e["source"] == nid)):
                    raise ValueError(f"Thread {tid} edge {inc} does not connect {prev_nid} -> {nid}")
        thread_ids.add(tid)

    return graph


if __name__ == "__main__":
    if len(sys.argv) > 1:
        data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        validate_graph(data)
        print(f"Schema OK: {len(data.get('syllabus', []))} syllabus items, {len(data['nodes'])} nodes, {len(data['edges'])} edges, {len(data['threads'])} threads.")
