"""交接清单：把门户确认过的事实与尚未确认的事项分开呈现给资料员。

只有“门户显示的文件集合”与“已保存并校验过的原件”完全相符时才标记收齐。
"""
from __future__ import annotations

import json
from pathlib import Path

from .model import (
    CLAIMED,
    COLLECTED,
    CONTEXT_MISMATCH,
    CREATE_PENDING,
    PARTIAL,
    PENDING_VERIFICATION,
    RECORDED,
    STATE_TEXT,
    BLOCKED,
    Store,
)

PART_LABELS = {"main": "正文", "supplement": "补充册"}


def _part_label(part: str) -> str:
    return PART_LABELS.get(part, part)


def build_manifest(store: Store) -> dict:
    """汇总当前所有需求的确认状态，供 JSON 与 Markdown 共用。"""
    items = []
    for req in store.requirements.values():
        confirmed = []
        unconfirmed = []
        if req.state == CONTEXT_MISMATCH:
            unconfirmed.append("上下文卡片字段与申请不一致，未消耗额度")
        if req.state == CREATE_PENDING:
            unconfirmed.append(
                "已点击申请导出但受理结果无法确定；门户无幂等键，恢复后须先查申请记录，禁止再次提交"
            )
        if req.state == PENDING_VERIFICATION:
            unconfirmed.append("申请记录中存在归属需人工判断的记录（如操作员手工申请），自动化未认领")
        if req.application_id:
            confirmed.append(f"门户申请记录确认申请号 {req.application_id}")
        if req.files:
            for f in req.files.values():
                confirmed.append(
                    f"已保存并校验原件：{_part_label(f.part)}（{f.filename}，sha256 {f.sha256[:12]}…）"
                )
        if req.missing:
            for part, reason in req.missing.items():
                unconfirmed.append(f"缺册 {_part_label(part)}：{reason}")
        if not req.application_id and req.state not in (CREATE_PENDING, CONTEXT_MISMATCH):
            unconfirmed.append("尚无门户申请号")
        items.append(
            {
                "key": req.key,
                "企业名称": req.label or "",
                "企业代码": req.code,
                "报告期": req.year,
                "披露范围": req.scope,
                "状态码": req.state,
                "状态说明": STATE_TEXT.get(req.state, req.state),
                "门户申请号": req.application_id,
                "已收齐": req.state == COLLECTED,
                "已保存原件": [
                    {
                        "册别": f.part,
                        "册别名称": _part_label(f.part),
                        "文件名": f.filename,
                        "相对路径": f.relative_path,
                        "sha256": f.sha256,
                        "字节数": f.size,
                        "原件内申请号": f.application_id,
                        "原件内企业": f.company,
                        "原件内报告期": f.year,
                        "原件内范围": f.scope,
                    }
                    for f in req.files.values()
                ],
                "缺册": [
                    {"册别": p, "册别名称": _part_label(p), "原因": why}
                    for p, why in req.missing.items()
                ],
                "门户已确认": confirmed,
                "尚未确认": unconfirmed,
                "证据文件": list(req.evidence),
                "备注": list(req.notes),
            }
        )
    return {
        "说明": "本清单仅依据门户可见页面与页面触发的下载生成；__admin 演练控制未用于任何判断",
        "需求数": len(items),
        "已收齐数": sum(1 for i in items if i["已收齐"]),
        "待操作员处理数": sum(
            1 for i in items if i["状态码"] in (CREATE_PENDING, PENDING_VERIFICATION, BLOCKED, CONTEXT_MISMATCH)
        ),
        "items": items,
    }


def write_handover(store: Store) -> Path:
    """写出每需求 JSON 清单与总 Markdown 交接单，返回交接目录。"""
    store.handover_dir.mkdir(parents=True, exist_ok=True)
    manifest = build_manifest(store)
    for item in manifest["items"]:
        d = store.handover_dir / item["key"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "manifest.json").write_text(
            json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (d / "manifest.md").write_text(_render_item_md(item), encoding="utf-8")
    (store.handover_dir / "summary.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (store.handover_dir / "summary.md").write_text(_render_summary_md(manifest), encoding="utf-8")
    return store.handover_dir


def _render_item_md(item: dict) -> str:
    lines = [
        f"# 资料需求 {item['key']}",
        "",
        f"- 企业：{item['企业名称'] or '（未填名称）'}",
        f"- 企业代码：{item['企业代码']}",
        f"- 报告期：{item['报告期']}",
        f"- 披露范围：{item['披露范围']}",
        f"- 门户申请号：{item['门户申请号'] or '（无）'}",
        f"- 当前状态：{item['状态说明']}",
        f"- 是否收齐：{'是' if item['已收齐'] else '否'}",
        "",
        "## 门户已确认的结论",
    ]
    lines += [f"- {x}" for x in item["门户已确认"]] or ["- （暂无）"]
    lines += ["", "## 尚未确认 / 待处理"]
    lines += [f"- {x}" for x in item["尚未确认"]] or ["- （暂无）"]
    lines += ["", "## 已保存原件"]
    if item["已保存原件"]:
        for f in item["已保存原件"]:
            lines.append(
                f"- {f['册别名称']}（{f['册别']}）：{f['相对路径']}，"
                f"sha256 `{f['sha256']}`，{f['字节数']} 字节"
            )
    else:
        lines.append("- （暂无原件）")
    lines += ["", "## 缺册"]
    lines += [f"- {x['册别名称']}：{x['原因']}" for x in item["缺册"]] or ["- （无）"]
    lines += ["", "## 来源证据（页面快照/截图）"]
    lines += [f"- {x}" for x in item["证据文件"]] or ["- （暂无）"]
    lines += ["", "## 备注"]
    lines += [f"- {x}" for x in item["备注"]] or ["- （无）"]
    lines.append("")
    return "\n".join(lines)


def _render_summary_md(manifest: dict) -> str:
    lines = [
        "# ESG 原件交接总单",
        "",
        f"- 需求总数：{manifest['需求数']}",
        f"- 已收齐：{manifest['已收齐数']}",
        f"- 待操作员处理：{manifest['待操作员处理数']}",
        "",
        "| 需求 | 申请号 | 状态 | 已保存/缺册 | 收齐 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for i in manifest["items"]:
        saved = "、".join(x["册别名称"] for x in i["已保存原件"]) or "无"
        missing = "、".join(x["册别名称"] for x in i["缺册"])
        saved_txt = saved + (f"；缺：{missing}" if missing else "")
        lines.append(
            f"| {i['key']} | {i['门户申请号'] or '—'} | {i['状态说明']} | {saved_txt} | "
            f"{'是' if i['已收齐'] else '否'} |"
        )
    lines += ["", "## 门户已确认的结论", ""]
    for i in manifest["items"]:
        for x in i["门户已确认"]:
            lines.append(f"- [{i['key']}] {x}")
    lines += ["", "## 尚未确认的事项", ""]
    pending = [(i, x) for i in manifest["items"] for x in i["尚未确认"]]
    lines += [f"- [{i['key']}] {x}" for i, x in pending] or ["- （无）"]
    lines.append("")
    return "\n".join(lines)
