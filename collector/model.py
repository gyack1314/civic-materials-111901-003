"""采集状态模型与持久化。

状态机只描述采集端自己掌握的事实，不替代门户判断：
- 门户已确认的事实（上下文卡片、申请记录、下载原件）逐条记录证据；
- 创建结果无法确定时停留 CREATE_PENDING，恢复后先查申请记录，绝不重复提交。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
from typing import Optional

# ---- 需求状态 -------------------------------------------------------------
RECORDED = "RECORDED"                      # 已登记需求，尚未在门户操作
CONTEXT_MISMATCH = "CONTEXT_MISMATCH"      # 上下文卡片与申请不符，禁止消耗额度
CREATE_PENDING = "CREATE_PENDING"          # 创建结果无法确定，待申请记录核实
PENDING_VERIFICATION = "PENDING_VERIFICATION"  # 待人工核实（如存在手工申请/归属歧义）
CLAIMED = "CLAIMED"                        # 已从门户申请记录确认归属，等待处理或下载
PARTIAL = "PARTIAL"                        # 已取得部分原件，仍有缺册
COLLECTED = "COLLECTED"                    # 门户文件集合与已保存原件完全相符
BLOCKED = "BLOCKED"                        # 门户持续异常等阻塞，需操作员处理

TERMINAL_OK = {COLLECTED}
# 可恢复运行后继续推进的状态（除 COLLECTED 外都允许再次核对）
RESUMABLE = {
    RECORDED, CONTEXT_MISMATCH, CREATE_PENDING, PENDING_VERIFICATION,
    CLAIMED, PARTIAL, BLOCKED,
}

STATE_TEXT = {
    RECORDED: "已登记，尚未在门户操作",
    CONTEXT_MISMATCH: "上下文卡片与申请不符，已阻止消耗额度",
    CREATE_PENDING: "创建结果无法确定，待申请记录核实（禁止重复提交）",
    PENDING_VERIFICATION: "待操作员核实（存在手工申请或归属歧义）",
    CLAIMED: "门户已受理，等待处理或下载",
    PARTIAL: "部分原件已取得，仍有缺册",
    COLLECTED: "门户文件集合与已保存原件相符，已收齐",
    BLOCKED: "门户持续异常，需操作员处理",
}


@dataclass
class SavedFile:
    """一份已保存原件的校验信息。"""
    part: str
    filename: str
    relative_path: str
    sha256: str
    size: int
    application_id: str
    company: str
    year: str
    scope: str


@dataclass
class Requirement:
    """一项资料需求（企业代码 + 报告期 + 披露范围，三者完全相同才是同一项）。"""
    code: str
    year: str
    scope: str
    label: str = ""
    state: str = RECORDED
    # 门户申请号；只有在页面申请记录中确认后才填写
    application_id: Optional[str] = None
    # 自己是否点过“申请导出”（用于区分创建不确定与从未创建）
    apply_attempted: bool = False
    # 创建不确定后，在证据充分（无匹配且无在办卡片）时允许的补建次数，上限一次
    rebuild_count: int = 0
    # 最近一次创建尝试是否收到门户“已有导出在办”(409)：在看到在办卡片变就绪前不补建
    saw_busy_guard: bool = False
    files: dict[str, SavedFile] = field(default_factory=dict)
    # 缺册 -> 最近一次原因（含门户响应状态等依据）
    missing: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    rounds: int = 0

    @property
    def key(self) -> str:
        return f"{self.code}-{self.year}-{self.scope}"

    def add_note(self, note: str) -> None:
        self.notes.append(note)

    def add_evidence(self, relative_path: str) -> None:
        if relative_path not in self.evidence:
            self.evidence.append(relative_path)


class Store:
    """工作目录下的采集状态，原子写入，进程中断不会留下半截状态文件。"""

    def __init__(self, workdir: str | Path):
        self.workdir = Path(workdir)
        self.originals_dir = self.workdir / "originals"
        self.evidence_dir = self.workdir / "evidence"
        self.handover_dir = self.workdir / "handover"
        self.state_path = self.workdir / "state.json"
        self.requirements: dict[str, Requirement] = {}

    # ---- 目录 ----
    def ensure_dirs(self) -> None:
        for d in (self.workdir, self.originals_dir, self.evidence_dir, self.handover_dir):
            d.mkdir(parents=True, exist_ok=True)

    def req_dirs(self, key: str) -> tuple[Path, Path]:
        originals = self.originals_dir / key
        evidence = self.evidence_dir / key
        originals.mkdir(parents=True, exist_ok=True)
        evidence.mkdir(parents=True, exist_ok=True)
        return originals, evidence

    # ---- 需求登记 ----
    def add_requirement(self, code: str, year: str, scope: str, label: str = "") -> Requirement:
        req = Requirement(code=code, year=str(year), scope=scope, label=label)
        existing = self.requirements.get(req.key)
        if existing is not None:
            return existing
        self.requirements[req.key] = req
        self.save()
        return req

    # ---- 持久化 ----
    def save(self) -> None:
        self.ensure_dirs()
        payload = {
            "version": 1,
            "requirements": [asdict(r) for r in self.requirements.values()],
        }
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)

    @classmethod
    def load(cls, workdir: str | Path) -> "Store":
        store = cls(workdir)
        if not store.state_path.exists():
            store.ensure_dirs()
            return store
        payload = json.loads(store.state_path.read_text(encoding="utf-8"))
        for raw in payload.get("requirements", []):
            files = {k: SavedFile(**v) for k, v in (raw.pop("files") or {}).items()}
            req = Requirement(**raw)
            req.files = files
            store.requirements[req.key] = req
        return store

    def pending(self) -> list[Requirement]:
        return [r for r in self.requirements.values() if r.state != COLLECTED]
