#!/usr/bin/env python3
"""Local asset bookkeeping. No network, credentials, generation, or image editing.

Python 3.10+. Pillow is optional for deeper read-only decoding validation.
All command results are JSON. Human/agent visual checks are separate from metadata.
"""
from __future__ import annotations

import argparse
import csv
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import struct
import sys
import uuid
import zipfile
import zlib

VERSION = 1
ROLES = {"product", "back", "detail", "style", "model", "document"}
KINDS = {"packshot", "scene", "feature", "detail", "size", "localized", "tryon", "combination", "other"}
CHECKS = {"identity", "text", "claims", "visual", "usage"}
RESULTS = {"pass", "fail", "unknown", "not_applicable"}
MAX_FILE = 100 * 1024 * 1024


class WorkflowError(ValueError):
    pass


def require(ok, message):
    if not ok:
        raise WorkflowError(message)


def now():
    return datetime.now(timezone.utc).isoformat()


def identifier(value):
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value),
            "编号请使用 1–64 位字母、数字、短横线或下划线，且以字母或数字开头。")
    return value


def read_json(path):
    p = Path(path)
    require(p.stat().st_size <= 10 * 1024 * 1024, "JSON 文件超过 10 MB。")
    return json.loads(p.read_text(encoding="utf-8"))


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def save_json(path, value):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, p)
    finally:
        tmp.unlink(missing_ok=True)


def child(root, relative):
    require(isinstance(relative, str) and relative, "缺少任务内文件路径。")
    rel = Path(relative)
    root = Path(root).resolve()
    require(not rel.is_absolute() and ".." not in rel.parts, "任务文件路径必须位于任务目录内。")
    p = (root / rel).resolve()
    require(p.is_relative_to(root), "任务内路径或软链接越出了任务目录。")
    return p


@contextmanager
def locked(run):
    lock = Path(run) / ".write-lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise WorkflowError("该任务有写操作进行中；若上次进程异常退出，请核实后移除 .write-lock。") from exc
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        lock.unlink(missing_ok=True)


def inspect_image(path):
    """Read structural metadata; never claim header validation proves visual fidelity."""
    p = Path(path)
    size = p.stat().st_size
    require(0 < size <= MAX_FILE, "图片为空或超过 100 MB。")
    b = p.read_bytes()
    width = height = 0
    alpha = None
    fmt = None
    if b.startswith(b"\x89PNG\r\n\x1a\n"):
        fmt = "PNG"
        pos, seen_data, ended = 8, False, False
        color = None
        while pos + 12 <= len(b):
            length = struct.unpack_from(">I", b, pos)[0]
            end = pos + 12 + length
            require(end <= len(b), "PNG 数据块不完整。")
            kind, payload = b[pos + 4:pos + 8], b[pos + 8:pos + 8 + length]
            crc = struct.unpack_from(">I", b, pos + 8 + length)[0]
            require(zlib.crc32(kind + payload) & 0xffffffff == crc, "PNG 数据块校验失败。")
            if pos == 8:
                require(kind == b"IHDR" and length == 13, "PNG 缺少有效 IHDR。")
                width, height = struct.unpack_from(">II", payload)
                color = payload[9]
                require(color in (0, 2, 3, 4, 6), "PNG 色彩类型不支持。")
                alpha = color in (4, 6)
            if kind == b"tRNS":
                alpha = True
            if kind == b"IDAT":
                seen_data = seen_data or bool(length)
            if kind == b"IEND":
                require(length == 0 and end == len(b), "PNG 结尾异常。")
                ended = True
                break
            pos = end
        require(seen_data and ended, "PNG 缺少图像数据或结尾。")
    elif b.startswith(b"\xff\xd8"):
        fmt, alpha = "JPEG", False
        require(b.endswith(b"\xff\xd9"), "JPEG 结尾不完整。")
        pos = 2
        while pos + 1 < len(b):
            require(b[pos] == 255, "JPEG 标记异常。")
            while pos < len(b) and b[pos] == 255:
                pos += 1
            require(pos < len(b), "JPEG 不完整。")
            marker = b[pos]
            pos += 1
            if marker in (0xD9, 0xDA):
                break
            if marker == 0x01 or 0xD0 <= marker <= 0xD8:
                continue
            require(pos + 2 <= len(b), "JPEG 数据块不完整。")
            length = struct.unpack_from(">H", b, pos)[0]
            require(length >= 2 and pos + length <= len(b), "JPEG 数据块长度异常。")
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                require(length >= 8, "JPEG 尺寸信息不完整。")
                height, width = struct.unpack_from(">HH", b, pos + 3)
            pos += length
    elif b.startswith(b"RIFF") and b[8:12] == b"WEBP":
        fmt = "WEBP"
        require(len(b) >= 30 and int.from_bytes(b[4:8], "little") + 8 == len(b), "WebP 数据长度异常。")
        kind, payload = b[12:16], b[20:]
        if kind == b"VP8X":
            alpha = bool(payload[0] & 0x10)
            width = 1 + int.from_bytes(payload[4:7], "little")
            height = 1 + int.from_bytes(payload[7:10], "little")
        elif kind == b"VP8L" and payload[0] == 0x2f:
            bits = int.from_bytes(payload[1:5], "little")
            width, height = (bits & 0x3fff) + 1, ((bits >> 14) & 0x3fff) + 1
            alpha = bool((bits >> 28) & 1)
        elif kind == b"VP8 " and payload[3:6] == b"\x9d\x01\x2a":
            width = int.from_bytes(payload[6:8], "little") & 0x3fff
            height = int.from_bytes(payload[8:10], "little") & 0x3fff
            alpha = False
    require(fmt is not None and width > 0 and height > 0, "仅支持有效的 PNG、JPEG 或 WebP 图片。")
    result = {"format": fmt, "width": width, "height": height, "bytes": size,
              "sha256": file_hash(p), "has_alpha_channel": alpha,
              "decode_check": "not_checked", "transparency_check": "not_checked",
              "scope": "文件结构与元数据；不证明商品一致、实际透明或平台审核通过。"}
    try:
        from PIL import Image
    except ImportError:
        return result
    try:
        with Image.open(p) as im:
            im.verify()
        with Image.open(p) as im:
            result["decode_check"] = "pass"
            result["color_mode"] = im.mode
            if im.width * im.height <= 20_000_000:
                a = im.convert("RGBA").getchannel("A")
                result["transparency_check"] = "transparent_pixels_present" if a.getextrema()[0] < 255 else "fully_opaque"
    except Exception as exc:
        raise WorkflowError("图片解码验证失败。") from exc
    return result


def validate_request(data, base):
    require(isinstance(data, dict) and data.get("schema_version") == VERSION, "请求 schema_version 必须为 1。")
    require(isinstance(data.get("products"), list) and data["products"], "请求需要非空 products。")
    require(isinstance(data.get("brand", {}), dict), "brand 必须为对象。")
    skus = set()
    products = []
    for product in data["products"]:
        require(isinstance(product, dict), "商品必须为对象。")
        sku = identifier(product.get("sku"))
        require(sku not in skus, "SKU 重复：" + sku)
        skus.add(sku)
        require(isinstance(product.get("name"), str) and product["name"].strip(), "商品需要名称。")
        refs, ids = [], set()
        for ref in product.get("references", []):
            rid = identifier(ref.get("id"))
            require(rid not in ids and ref.get("role") in ROLES, "参考图编号重复或角色无效。")
            ids.add(rid)
            path = Path(ref["path"]).expanduser()
            if not path.is_absolute():
                path = Path(base) / path
            path = path.resolve()
            require(path.is_file(), "参考文件不存在：" + str(path))
            require(0 < path.stat().st_size <= MAX_FILE, "参考文件为空或过大。")
            info = inspect_image(path) if ref["role"] != "document" else None
            refs.append({"id": rid, "role": ref["role"], "source_path": str(path),
                         "sha256": file_hash(path), "image_info": info})
        require(any(r["role"] in {"product", "back", "detail"} for r in refs), "图片任务需要至少一张真实商品参考；风格图不能充当商品原图。")
        facts, fact_ids = [], set()
        for fact in product.get("facts", []):
            fid = identifier(fact.get("id"))
            require(fid not in fact_ids, "事实编号重复。")
            fact_ids.add(fid)
            require(fact.get("status") in {"observed", "confirmed", "unknown"}, "事实状态无效。")
            require(isinstance(fact.get("value"), str), "事实 value 必须为文本。")
            source = fact.get("source", {})
            require(isinstance(source, dict) and source.get("kind") in {"user", "image", "document", "unknown"}, "事实需要来源类别。")
            require(isinstance(source.get("ref"), str) and source["ref"].strip(), "事实需要可定位的来源。")
            if source["kind"] in {"image", "document"}:
                require(source["ref"] in ids, "事实引用了未提供的参考文件。")
                referenced = next(r for r in refs if r["id"] == source["ref"])
                if source["kind"] == "image":
                    require(referenced["role"] in {"product", "back", "detail"}, "风格图或人物图不能作为商品事实来源。")
                else:
                    require(referenced["role"] == "document", "文档事实必须引用 document 角色的资料。")
            if fact["status"] != "unknown":
                require(source["kind"] != "unknown" and fact["value"].strip(), "已知事实需要有效来源与内容。")
            facts.append(fact)
        known = {f["id"] for f in facts if f["status"] != "unknown"}
        shots, shot_ids = [], set()
        for shot in product.get("shots", []):
            sid = identifier(shot.get("id"))
            require(sid not in shot_ids and shot.get("kind") in KINDS, "图位编号重复或类型无效。")
            shot_ids.add(sid)
            require(isinstance(shot.get("goal"), str) and shot["goal"].strip(), "图位需要制作目标。")
            require(isinstance(shot.get("text", []), list) and all(isinstance(x, str) for x in shot.get("text", [])), "图中文字必须为文本列表。")
            claims = shot.get("fact_ids", [])
            require(isinstance(claims, list) and all(isinstance(x, str) for x in claims) and set(claims) <= known, "图位不能引用未知或不存在的商品事实。")
            aspect = shot.get("aspect", "1:1")
            require(isinstance(aspect, str) and re.fullmatch(r"[1-9][0-9]?:[1-9][0-9]?", aspect), "比例请写为 1:1、3:4 等。")
            limit = shot.get("max_renders", 1)
            require(type(limit) is int and 1 <= limit <= 20, "max_renders 应为 1–20。")
            shots.append({"id": sid, "kind": shot["kind"], "goal": shot["goal"], "text": shot.get("text", []),
                          "fact_ids": claims, "aspect": aspect, "max_renders": limit})
        require(shots, "每个商品至少需要一个图位。")
        invariants = product.get("invariants", [])
        require(isinstance(invariants, list) and all(isinstance(x, str) for x in invariants), "invariants 必须为文本列表。")
        products.append({"sku": sku, "name": product["name"], "references": refs, "facts": facts,
                         "invariants": invariants, "shots": shots})
    return {"schema_version": VERSION, "label": str(data.get("label", "商品素材任务")),
            "brand": data.get("brand", {}), "products": products}


def init_run(request_file, runs):
    request_file = Path(request_file).resolve()
    plan = validate_request(read_json(request_file), request_file.parent)
    run = Path(runs).expanduser().resolve() / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:10])
    run.mkdir(parents=True, exist_ok=False)
    try:
        for p in plan["products"]:
            for ref in p["references"]:
                suffix = Path(ref["source_path"]).suffix.lower()
                if ref["image_info"]:
                    suffix = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}[ref["image_info"]["format"]]
                ref["snapshot"] = "inputs/" + p["sku"] + "/" + ref["id"] + suffix
                target = child(run, ref["snapshot"])
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ref["source_path"], target)
                require(file_hash(target) == ref["sha256"], "复制过程中原图变化，请重新建任务。")
        save_json(run / "plan.json", plan)
        state = {"schema_version": VERSION, "created_at": now(), "plan_sha256": digest(plan), "jobs": {}}
        for p in plan["products"]:
            for s in p["shots"]:
                state["jobs"][p["sku"] + "/" + s["id"]] = {"revisions": [], "events": []}
        save_json(run / "state.json", state)
    except Exception:
        shutil.rmtree(run)
        raise
    return {"run": str(run), "jobs": len(state["jobs"]), "next": "由 Codex 查看原图并调用内置图片工具；本程序不生图。"}


def load_run(run, check_source=False):
    run = Path(run).expanduser().resolve()
    plan, state = read_json(run / "plan.json"), read_json(run / "state.json")
    require(plan.get("schema_version") == VERSION and state.get("schema_version") == VERSION, "任务版本不支持。")
    require(digest(plan) == state["plan_sha256"], "商品或品牌计划已被修改；请用更新后的请求新建任务，保留原任务。")
    for p in plan["products"]:
        for ref in p["references"]:
            snap = child(run, ref["snapshot"])
            require(snap.is_file() and file_hash(snap) == ref["sha256"], "原图快照缺失或被修改：" + p["sku"] + "/" + ref["id"])
            original = Path(ref["source_path"])
            if check_source and original.is_file():
                require(file_hash(original) == ref["sha256"], "外部原图已更新；请新建任务，不复用旧图判断：" + p["sku"] + "/" + ref["id"])
    return run, plan, state


def get_job(plan, state, sku, shot):
    key = identifier(sku) + "/" + identifier(shot)
    require(key in state["jobs"], "任务中没有这个商品／图位。")
    p = next(p for p in plan["products"] if p["sku"] == sku)
    s = next(s for s in p["shots"] if s["id"] == shot)
    return p, s, state["jobs"][key]


def revision_at(run, job, version=None):
    require(job["revisions"], "尚未记录图片结果。")
    revision = job["revisions"][-1] if version is None else next((r for r in job["revisions"] if r["version"] == version), None)
    require(revision is not None, "没有这个图片版本。")
    path = child(run, revision["file"])
    require(path.is_file() and file_hash(path) == revision["sha256"], "结果文件缺失或已变化，不能沿用旧审核。")
    return revision


def latest(run, job):
    return revision_at(run, job)


def selected_revision(run, job):
    selected = [r for r in job["revisions"] if r["selection"] == "selected"]
    require(len(selected) <= 1, "同一图位有多个选用版本，请重新明确选择。")
    return revision_at(run, job, selected[0]["version"]) if selected else None


def show_job(run, sku, shot):
    run, plan, state = load_run(run, check_source=True)
    p, s, job = get_job(plan, state, sku, shot)
    history = []
    for entry in job["revisions"]:
        r = revision_at(run, job, entry["version"])
        history.append({"version": r["version"], "path": str(child(run, r["file"])),
                        "sha256": r["sha256"], "review_status": r["review_status"], "selection": r["selection"]})
    return {"sku": sku, "name": p["name"], "shot": s, "brand": plan["brand"],
            "facts": p["facts"], "invariants": p["invariants"],
            "references": [{"id": r["id"], "role": r["role"], "path": str(child(run, r["snapshot"])), "sha256": r["sha256"]} for r in p["references"]],
            "history": history, "events": job["events"]}


def import_csv(csv_file, output, shot_kinds="packshot,feature,scene", brand_file=None):
    source = Path(csv_file).expanduser().resolve()
    output = Path(output).expanduser().resolve()
    require(not output.exists(), "请求文件已存在，请使用新的文件名。")
    kinds = shot_kinds.split(",")
    require(kinds and len(set(kinds)) == len(kinds) and all(k in KINDS for k in kinds), "图位类型无效或重复。")
    products, seen = [], set()
    with source.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        require(reader.fieldnames is not None and {"sku", "name", "image"} <= set(reader.fieldnames), "CSV 至少需要 sku、name、image 三列。")
        for line, row in enumerate(reader, 2):
            require(None not in row, f"CSV 第 {line} 行列数异常。")
            sku = identifier((row.get("sku") or "").strip())
            require(sku not in seen, f"CSV 第 {line} 行 SKU 重复：{sku}")
            seen.add(sku)
            name = (row.get("name") or "").strip()
            image = (row.get("image") or "").strip()
            require(name and image and "://" not in image, f"CSV 第 {line} 行需要名称和本地图片路径。远程图片先由 Codex 收集为本地文件。")
            path = Path(image).expanduser()
            if not path.is_absolute(): path = source.parent / path
            require(path.is_file(), f"CSV 第 {line} 行图片不存在：{path}")
            facts = []
            if (row.get("description") or "").strip():
                facts.append({"id": "supplier_description", "value": row["description"].strip(), "status": "unknown",
                              "source": {"kind": "document", "ref": "source_csv"}})
            products.append({"sku": sku, "name": name,
                             "references": [{"id": "front", "role": "product", "path": str(path.resolve())},
                                            {"id": "source_csv", "role": "document", "path": str(source)}],
                             "facts": facts, "invariants": [],
                             "shots": [{"id": k, "kind": k, "goal": "待 Codex 根据原图与确认资料细化：" + k,
                                        "text": [], "fact_ids": [], "aspect": "1:1", "max_renders": 1} for k in kinds]})
    data = {"schema_version": VERSION, "label": source.stem + " 商品素材", "products": products,
            "brand": read_json(brand_file) if brand_file else {}}
    validate_request(data, output.parent)
    save_json(output, data)
    return {"request": str(output), "products": len(products), "shots": len(kinds) * len(products),
            "next": "这是结构化草稿。Codex 需查看原图、补充真实特征和图位目标，再初始化任务；供应商描述未自动确认。"}


def attempt_count(job):
    return len(job["revisions"]) + sum(e["event"] == "failed" for e in job["events"])


def log_failure(run, sku, shot, event, note):
    require(event in {"failed", "blocked"} and note.strip(), "需要失败／受阻类型和实际原因。")
    run = Path(run).resolve()
    with locked(run):
        run, plan, state = load_run(run)
        p, s, job = get_job(plan, state, sku, shot)
        job["events"].append({"at": now(), "event": event, "note": note})
        save_json(run / "state.json", state)
        return {"status": event, "attempts": attempt_count(job), "retained_versions": len(job["revisions"])}


def status_run(run):
    run, plan, state = load_run(run, check_source=True)
    jobs = []
    for p in plan["products"]:
        for s in p["shots"]:
            job = state["jobs"][p["sku"] + "/" + s["id"]]
            stage, action = "planned", "generate"
            if job["revisions"]:
                r = latest(run, job)
                stage = r["review_status"]
                action = "review" if stage == "not_reviewed" else "ready"
                if stage == "needs_revision":
                    action = "revise" if attempt_count(job) < s["max_renders"] else "needs_input"
                if stage == "inconclusive":
                    action = "needs_input"
                if r["selection"] == "rejected":
                    action = "needs_input"
            if job["events"] and job["events"][-1]["event"] in {"failed", "blocked"}:
                stage = job["events"][-1]["event"]
                action = "needs_input" if stage == "blocked" or attempt_count(job) >= s["max_renders"] else "revise" if job["revisions"] else "generate"
            chosen = selected_revision(run, job)
            if chosen and job["events"] and job["events"][-1]["event"] == "selected":
                stage = chosen["review_status"]
                action = "ready" if stage == "reviewed_candidate" else "needs_input"
            jobs.append({"sku": p["sku"], "shot": s["id"], "status": stage, "next_action": action,
                         "selected_version": chosen["version"] if chosen else None,
                         "renders": len(job["revisions"]), "attempts": attempt_count(job), "max_renders": s["max_renders"]})
    return {"run": str(run), "jobs": jobs}


def record(run, sku, shot, image, prompt_file):
    run = Path(run).resolve()
    with locked(run):
        run, plan, state = load_run(run, check_source=True)
        p, s, job = get_job(plan, state, sku, shot)
        info = inspect_image(image)
        prompt = Path(prompt_file).read_text(encoding="utf-8").strip()
        require(prompt, "记录实际使用的非空制作说明。")
        if job["revisions"]:
            old = latest(run, job)
            if old["sha256"] == info["sha256"] and old["prompt_sha256"] == digest(prompt):
                return {"revision": old, "idempotent": True}
        require(attempt_count(job) < s["max_renders"], "已到本图制作次数上限；先取得新的制作范围并新建任务。")
        number = len(job["revisions"]) + 1
        ext = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}[info["format"]]
        rel = f"images/{sku}/{shot}-v{number}{ext}"
        target = child(run, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        require(not target.exists(), "目标版本文件已经存在，不覆盖。")
        revision = {"version": number, "file": rel, "sha256": info["sha256"], "metadata": info,
                    "prompt": prompt, "prompt_sha256": digest(prompt), "recorded_at": now(),
                    "review_status": "not_reviewed", "reviews": [], "selection": "not_selected", "selection_events": []}
        try:
            shutil.copyfile(image, target)
            require(file_hash(target) == info["sha256"], "复制过程中结果变化，请重新记录。")
            job["revisions"].append(revision)
            job["events"].append({"at": now(), "event": "generated", "version": number})
            save_json(run / "state.json", state)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return {"revision": revision, "idempotent": False}


def review_template(run, sku, shot, version=None):
    run, plan, state = load_run(run)
    p, s, job = get_job(plan, state, sku, shot)
    r = revision_at(run, job, version)
    return {"candidate_sha256": r["sha256"],
            "reference_sha256": {x["id"]: x["sha256"] for x in p["references"]},
            "reviewer": "Codex visual review", "checks": {
                k: {"status": "unknown", "evidence": "填写查看原图与候选图后的具体依据。"} for k in sorted(CHECKS)}}


def review(run, sku, shot, review_file, version=None):
    report = read_json(review_file)
    run = Path(run).resolve()
    with locked(run):
        run, plan, state = load_run(run, check_source=True)
        p, s, job = get_job(plan, state, sku, shot)
        r = revision_at(run, job, version)
        require(report.get("candidate_sha256") == r["sha256"], "审核对应的结果版本已变化。")
        require(report.get("reference_sha256") == {x["id"]: x["sha256"] for x in p["references"]}, "审核必须包含本商品的全部参考快照，且版本一致。")
        checks = report.get("checks", {})
        require(isinstance(checks, dict) and set(checks) == CHECKS, "审核需要 identity、text、claims、visual、usage 五项。")
        for key, value in checks.items():
            require(isinstance(value, dict) and value.get("status") in RESULTS, "审核结论无效。")
            require(isinstance(value.get("evidence"), str) and value["evidence"].strip() and not value["evidence"].startswith("填写查看"), "每项审核需要实际依据，不能提交空模板。")
            if key in {"identity", "visual"}:
                require(value["status"] != "not_applicable", "商品一致性与视觉检查不能标为不适用。")
            if key == "text" and s["text"]:
                require(value["status"] != "not_applicable", "图位包含指定文字，不能跳过文字检查。")
        outcomes = {x["status"] for x in checks.values()}
        verdict = "needs_revision" if "fail" in outcomes else "inconclusive" if "unknown" in outcomes else "reviewed_candidate"
        r["reviews"].append({"at": now(), "report": report, "verdict": verdict})
        r["review_status"] = verdict
        save_json(run / "state.json", state)
        return {"review_status": verdict, "selection": r["selection"], "scope": "已记录视觉判断，不是平台或商品真实性认证。"}


def select(run, sku, shot, decision, note, allow_unreviewed=False, version=None):
    require(decision in {"selected", "rejected"} and note.strip(), "选择需要结论和用户选择依据。")
    run = Path(run).resolve()
    with locked(run):
        run, plan, state = load_run(run, check_source=True)
        p, s, job = get_job(plan, state, sku, shot)
        r = revision_at(run, job, version)
        require(decision != "selected" or r["review_status"] == "reviewed_candidate" or allow_unreviewed,
                "该图尚未通过已覆盖的检查；用户明确选择时可使用 --allow-unreviewed，并记录依据。")
        if decision == "selected":
            for other in job["revisions"]:
                if other is not r and other["selection"] == "selected":
                    other["selection"] = "not_selected"
                    other["selection_events"].append({"at": now(), "decision": "not_selected", "note": "用户改选其他版本", "override": False})
        r["selection"] = decision
        r["selection_events"].append({"at": now(), "decision": decision, "note": note, "override": bool(allow_unreviewed)})
        job["events"].append({"at": now(), "event": decision, "version": r["version"]})
        save_json(run / "state.json", state)
        return {"selection": decision, "version": r["version"], "review_status": r["review_status"]}


def check_profile(image, profile_file):
    meta = inspect_image(image)
    profile = read_json(profile_file)
    require(isinstance(profile, dict) and isinstance(profile.get("rules"), dict), "规格配置需要 rules 对象。")
    require(all(isinstance(profile.get(k), str) and profile[k] for k in ["name", "scope", "basis", "checked_at"]), "规格配置需要 name、scope、basis、checked_at，以区分项目要求与平台规则。")
    checks = []
    for key, value in profile["rules"].items():
        ok, actual = None, None
        if key in {"min_width", "min_height", "max_bytes"}:
            require(type(value) is int and value > 0, "尺寸和体积阈值应为正整数。")
            actual = meta[{"min_width": "width", "min_height": "height", "max_bytes": "bytes"}[key]]
            ok = actual <= value if key == "max_bytes" else actual >= value
        elif key == "formats":
            require(isinstance(value, list) and value and all(v in {"PNG", "JPEG", "WEBP"} for v in value), "formats 需要已支持格式列表。")
            actual, ok = meta["format"], meta["format"] in value
        elif key == "aspect":
            require(isinstance(value, str) and re.fullmatch(r"[1-9][0-9]?:[1-9][0-9]?", value), "比例配置无效。")
            w, h = map(int, value.split(":"))
            actual, ok = f"{meta['width']}:{meta['height']}", meta["width"] * h == meta["height"] * w
        elif key == "opaque":
            require(type(value) is bool, "opaque 需要布尔值。")
            actual = meta["transparency_check"]
            if not value:
                ok = True
            elif meta["has_alpha_channel"] is False or actual == "fully_opaque":
                ok = True
            elif actual == "transparent_pixels_present":
                ok = False
        checks.append({"check": key, "status": "unknown" if ok is None else "pass" if ok else "fail", "actual": actual, "expected": value})
    require(checks, "规格配置需要至少一条规则。")
    statuses = {c["status"] for c in checks}
    verdict = "fail" if "fail" in statuses else "inconclusive" if "unknown" in statuses else "covered_checks_pass"
    return {"profile": profile, "metadata": meta, "checks": checks, "verdict": verdict,
            "scope": "仅报告配置内的可测量项目；不等于平台审核通过。"}


def export_run(run, output, drafts=False, selected_only=False):
    run, plan, state = load_run(run, check_source=True)
    output = Path(output).expanduser().resolve()
    require(not output.exists() and not Path(str(output) + ".zip").exists(), "交付目录或 ZIP 已存在，请换一个版本名。")
    require(not output.is_relative_to(run), "交付目录应位于运行任务之外。")
    rows, omitted = [], []
    for p in plan["products"]:
        for s in p["shots"]:
            job = state["jobs"][p["sku"] + "/" + s["id"]]
            if job["revisions"]:
                chosen = selected_revision(run, job)
                r = chosen or latest(run, job)
                allowed = not selected_only or chosen is not None
                if allowed and (drafts or (r["review_status"] == "reviewed_candidate" and r["selection"] != "rejected")):
                    rows.append((p, s, r))
                else:
                    omitted.append({"sku": p["sku"], "shot": s["id"], "reason": "not_selected" if not allowed else r["review_status"], "selection": r["selection"]})
            else:
                reason = job["events"][-1]["event"] if job["events"] else "planned"
                omitted.append({"sku": p["sku"], "shot": s["id"], "reason": reason})
    require(rows, "没有可交付候选；先审核，或使用 --drafts 明确导出带状态的草稿。")
    tmp = output.with_name(output.name + ".tmp-" + uuid.uuid4().hex[:8])
    tmp.mkdir(parents=True, exist_ok=False)
    entries, cards = [], []
    try:
        for p, s, r in rows:
            rel = r["file"]
            target = child(tmp, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(child(run, rel), target)
            require(file_hash(target) == r["sha256"], "结果在导出过程中被修改。")
            refs = []
            for ref in p["references"]:
                refpath = "references/" + p["sku"] + "/" + Path(ref["snapshot"]).name
                dst = child(tmp, refpath)
                dst.parent.mkdir(parents=True, exist_ok=True)
                if not dst.exists():
                    shutil.copyfile(child(run, ref["snapshot"]), dst)
                require(file_hash(dst) == ref["sha256"], "参考文件在导出过程中被修改。")
                refs.append({"id": ref["id"], "role": ref["role"], "file": refpath, "sha256": ref["sha256"]})
            entry = {"sku": p["sku"], "product": p["name"], "shot": s, "version": r["version"], "file": rel, "sha256": r["sha256"],
                     "review_status": r["review_status"], "selection": r["selection"], "references": refs,
                     "review": r["reviews"][-1]["report"] if r["reviews"] else None}
            entries.append(entry)
            orig = next(x for x in refs if x["role"] in {"product", "back", "detail"})
            label = {"reviewed_candidate": "已核对 · 待确认", "inconclusive": "有待核对", "needs_revision": "需修改", "not_reviewed": "未审核"}[r["review_status"]]
            if r["selection"] == "selected":
                label = "用户已选用 · 已核对" if r["review_status"] == "reviewed_candidate" else "用户已选用 · " + label
            if r["selection"] == "rejected":
                label = "用户未采用 · " + label
            details = ""
            if entry["review"]:
                titles = {"identity": "商品一致性", "text": "文字", "claims": "卖点依据", "visual": "视觉", "usage": "使用范围"}
                details = "<ul>" + "".join("<li><b>" + titles[k] + "</b> " + html.escape(v["evidence"]) + "</li>" for k, v in entry["review"]["checks"].items()) + "</ul>"
            cards.append(f'<article><div class="row"><h2>{html.escape(p["name"])} · {html.escape(s["id"])}</h2><span>{html.escape(label)}</span></div><p>{html.escape(s["goal"])}</p><div class="pair"><figure><img src="{html.escape(orig["file"], quote=True)}" alt="商品原图"><figcaption>原图参考</figcaption></figure><figure><img src="{html.escape(rel, quote=True)}" alt="候选图"><figcaption>候选 v{r["version"]} · <a href="{html.escape(rel, quote=True)}" download>保存图片</a></figcaption></figure></div>{details}</article>')
        save_json(tmp / "manifest.json", {"schema_version": VERSION, "label": plan["label"], "created_at": now(), "draft_export": drafts, "selected_only": selected_only, "items": entries, "omitted": omitted})
        page = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>商品素材交付</title><style>body{margin:0;background:#f5f5f2;color:#242422;font:15px/1.65 system-ui,-apple-system,sans-serif}main{max-width:1100px;margin:52px auto;padding:0 24px}h1{font-size:32px;margin:0 0 8px;letter-spacing:-1px}h2{font-size:19px;margin:0}.intro{color:#61615a;margin-bottom:28px}.row{display:flex;gap:16px;justify-content:space-between;align-items:center;flex-wrap:wrap}.row span{font-size:13px;color:#476454}article{background:white;border:1px solid #deded7;padding:24px;margin:24px 0}.pair{display:grid;grid-template-columns:1fr 1fr;gap:20px}figure{margin:0}img{display:block;width:100%;aspect-ratio:1;object-fit:contain;background:#fafaf8}figcaption{padding:10px 0;color:#666}a{color:#315b50}li{padding:3px 0}footer{margin:30px 0;color:#6a6a62;font-size:13px}@media(max-width:640px){main{margin-top:28px}.pair{grid-template-columns:1fr}article{padding:16px}}</style><main><h1>' + html.escape(plan["label"]) + '</h1><p class="intro">原图与候选图对照 · ' + str(len(rows)) + ' 张素材。检查结果仅覆盖记录项目，最终采用由卖家决定。</p>' + ''.join(cards) + '<footer>文件清单：<a href="manifest.json">manifest.json</a> · 此预览不联网，不包含店铺登录信息。</footer></main></html>'
        if omitted:
            pending = '<section><h2>另有 ' + str(len(omitted)) + ' 项未纳入候选图片</h2><ul>' + ''.join('<li>' + html.escape(x['sku'] + ' / ' + x['shot'] + '：' + x['reason']) + '</li>' for x in omitted) + '</ul></section>'
            page = page.replace('<footer>', pending + '<footer>')
        (tmp / "index.html").write_text(page, encoding="utf-8")
        os.replace(tmp, output)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    archive = Path(str(output) + ".zip")
    with zipfile.ZipFile(archive, "x", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(output.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(output))
    return {"directory": str(output), "preview": str(output / "index.html"), "archive": str(archive), "count": len(rows), "drafts": drafts}


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init"); init.add_argument("--request", required=True); init.add_argument("--runs", required=True)
    stat = sub.add_parser("status"); stat.add_argument("--run", required=True)
    imp = sub.add_parser("import-csv"); imp.add_argument("--csv", required=True); imp.add_argument("--out", required=True)
    imp.add_argument("--shots", default="packshot,feature,scene"); imp.add_argument("--brand")
    for name in ["record", "review-template", "review", "select", "failure", "show"]:
        sp = sub.add_parser(name)
        for k in ["run", "sku", "shot"]: sp.add_argument("--" + k, required=True)
        if name in {"review-template", "review", "select"}: sp.add_argument("--version", type=int)
        if name == "record":
            sp.add_argument("--image", required=True); sp.add_argument("--prompt-file", required=True)
        elif name == "review": sp.add_argument("--report", required=True)
        elif name == "select":
            sp.add_argument("--decision", choices=["selected", "rejected"], required=True)
            sp.add_argument("--note", required=True); sp.add_argument("--allow-unreviewed", action="store_true")
        elif name == "failure":
            sp.add_argument("--event", choices=["failed", "blocked"], required=True)
            sp.add_argument("--note", required=True)
    exp = sub.add_parser("export"); exp.add_argument("--run", required=True); exp.add_argument("--out", required=True); exp.add_argument("--drafts", action="store_true"); exp.add_argument("--selected-only", action="store_true")
    ins = sub.add_parser("inspect"); ins.add_argument("paths", nargs="+")
    check = sub.add_parser("check"); check.add_argument("--image", required=True); check.add_argument("--profile", required=True)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        cmd = args.command
        if cmd == "init": result = init_run(args.request, args.runs)
        elif cmd == "import-csv": result = import_csv(args.csv, args.out, args.shots, args.brand)
        elif cmd == "status": result = status_run(args.run)
        elif cmd == "show": result = show_job(args.run, args.sku, args.shot)
        elif cmd == "record": result = record(args.run, args.sku, args.shot, args.image, args.prompt_file)
        elif cmd == "review-template": result = review_template(args.run, args.sku, args.shot, args.version)
        elif cmd == "review": result = review(args.run, args.sku, args.shot, args.report, args.version)
        elif cmd == "select": result = select(args.run, args.sku, args.shot, args.decision, args.note, args.allow_unreviewed, args.version)
        elif cmd == "failure": result = log_failure(args.run, args.sku, args.shot, args.event, args.note)
        elif cmd == "export": result = export_run(args.run, args.out, args.drafts, args.selected_only)
        elif cmd == "check": result = check_profile(args.image, args.profile)
        else: result = [{"path": str(Path(p).resolve()), "metadata": inspect_image(p)} for p in args.paths]
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, indent=2))
        return 0
    except (WorkflowError, OSError, ValueError, KeyError, TypeError, StopIteration, struct.error) as exc:
        print(json.dumps({"ok": False, "error": str(exc) or type(exc).__name__}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
