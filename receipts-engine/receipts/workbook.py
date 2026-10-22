"""Fill the submission workbook's yellow input cells only (BUILD_SPEC 9.6).

Every planned write is checked first: the target must be a yellow input cell and must not hold a
formula. Any mismatch aborts with the full list so a human can fix the mapping; nothing is saved.
"""
from __future__ import annotations

import csv
import re
import shutil
import subprocess
from pathlib import Path

import yaml
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from receipts.models import LEAD_COLUMNS

LEAD_SHEET = "02_LEADS"
LEAD_HEADER_ROW = 4
LEAD_FIRST_ROW = 5
WB_LEAD_COLUMNS = [c for c in LEAD_COLUMNS if c not in ("duplicate_check", "completeness_check")]  # A..AB


class WorkbookMappingError(Exception):
    def __init__(self, mismatches: list[str]):
        super().__init__("workbook mapping mismatches (nothing written):\n  " + "\n  ".join(mismatches))
        self.mismatches = mismatches


def cell_colour(cell) -> str:
    fg = cell.fill.fgColor if cell.fill is not None else None
    if fg is None:
        return "none"
    if fg.type == "rgb":
        return str(fg.rgb)
    if fg.type == "theme":
        return f"theme:{fg.theme}:tint:{round(fg.tint or 0, 2)}"
    if fg.type == "indexed":
        return f"indexed:{fg.indexed}"
    return str(fg.type)


def is_yellow(cell, s) -> bool:
    c = cell_colour(cell).upper()
    return c in {x.upper() for x in s.workbook.yellow_rgbs + s.workbook.yellow_other}


def is_formula(v) -> bool:
    return isinstance(v, str) and v.startswith("=")


def _exists(ref: str) -> bool:
    ref = (ref or "").strip()
    if not ref or "TODO" in ref:
        return False
    if re.match(r"https?://\S+", ref):
        return True
    return Path(ref).exists()


def _yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text()) or {} if path.exists() else {}


def _launch_kit_answers(path: Path) -> list[str]:
    """First paragraph under each numbered '## n)' / '## n.' heading, in order."""
    if not path.exists():
        return []
    out, cur = [], None
    for line in path.read_text().splitlines():
        if re.match(r"^#{1,3}\s*\d+[.)]", line):
            if cur is not None:
                out.append(" ".join(cur).strip())
            cur = []
        elif cur is not None and line.strip() and not line.startswith("#"):
            cur.append(line.strip())
    if cur is not None:
        out.append(" ".join(cur).strip())
    return out


def plan_writes(db, s, leads_csv: Path, cfg_dir: Path) -> tuple[dict[tuple[str, str], object], int]:
    writes: dict[tuple[str, str], object] = {}
    n_rows = 0
    if leads_csv.exists():
        with leads_csv.open(encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        rows.sort(key=lambda r: r["lead_id"])
        n_rows = len(rows)
        for i, r in enumerate(rows[: s.workbook.rows_max]):
            for j, col in enumerate(WB_LEAD_COLUMNS):
                v = r[col]
                if col in ("content_count_30d", "longform_count_60d") and v.isdigit():
                    v = int(v)
                elif col == "lead_score":
                    v = float(v)
                elif col == "followers_or_subscribers" and v.isdigit():
                    v = int(v)
                writes[(LEAD_SHEET, f"{get_column_letter(j + 1)}{LEAD_FIRST_ROW + i}")] = v
    sub = _yaml(cfg_dir / "submission.yaml")
    for coord, v in (sub.get("start") or {}).items():
        writes[("00_START", coord)] = v
    if sub.get("accept_declaration") is True:
        writes[("00_START", "B21")] = "Yes"
    for coord, ref in (sub.get("checklist") or {}).items():
        writes[("00_START", coord)] = "Yes" if _exists(str(ref)) else "No"
    ins = _yaml(cfg_dir / "insight.yaml")
    for base, key in ((6, "observations"), (14, "opportunities")):
        for i, row in enumerate(ins.get(key) or []):
            for j, v in enumerate(row[:5]):
                writes[("01_INSIGHT", f"{get_column_letter(2 + j)}{base + i}")] = v
    for row, req in (sub.get("engine_requirements") or {}).items():
        ev = str((req or {}).get("evidence", ""))
        writes[("03_ENGINE_LAUNCH", f"B{row}")] = "Yes" if _exists(ev) else "No"
        writes[("03_ENGINE_LAUNCH", f"C{row}")] = ev
        writes[("03_ENGINE_LAUNCH", f"H{row}")] = (req or {}).get("note", "")
    for row, vals in zip(range(18, 22), run_rows(db), strict=False):
        for j, v in enumerate(vals):
            writes[("03_ENGINE_LAUNCH", f"{get_column_letter(2 + j)}{row}")] = v
    for i, ans in enumerate(_launch_kit_answers(Path(sub.get("launch_kit_path", "launch_kit.md")))[:8]):
        writes[("03_ENGINE_LAUNCH", f"B{24 + i}")] = ans
    from receipts.stats import compute

    st = compute(db, s)
    for i, (val, ev) in enumerate(self_check_values(st, s, n_rows)):
        writes[("04_SELF_CHECK", f"C{6 + i}")] = val
        writes[("04_SELF_CHECK", f"E{6 + i}")] = ev
    if n_rows > s.workbook.rows_max:
        writes[("04_SELF_CHECK", "E6")] = (f"leads.csv has {n_rows} rows; workbook shows the first "
                                           f"{s.workbook.rows_max:,} as instructed")
    sc = _yaml(cfg_dir / "selfscore.yaml")
    for row, v in (sc.get("scores") or {}).items():
        writes[("04_SELF_CHECK", f"C{row}")] = (v or {}).get("score", "")
        writes[("04_SELF_CHECK", f"E{row}")] = (v or {}).get("reason", "")
    return writes, n_rows


def run_rows(db) -> list[list]:
    """Rows 18-21: normal run, repeat run, refresh, failure+recovery (latest of each), columns B:H."""
    def pick(sql):
        r = db.fetch_one(sql)
        if not r:
            return ["not run yet", "", "", "", "", "", ""]
        return [f"run {r['run_id']} ({r['kind']})", r["started"][:16], r["input_size"], r["produced"], r["rejected"],
                r["dupes_removed"], r["status"]]
    return [pick("SELECT * FROM runs WHERE kind='run' AND status='ok' AND produced>0 ORDER BY run_id LIMIT 1"),
            pick("SELECT * FROM runs WHERE kind='run' AND produced=0 AND unchanged>0 ORDER BY run_id DESC LIMIT 1"),
            pick("SELECT * FROM runs WHERE kind='refresh' ORDER BY run_id DESC LIMIT 1"),
            pick("SELECT * FROM runs WHERE status='partial' ORDER BY run_id DESC LIMIT 1")]


def self_check_values(st, s, n_rows: int) -> list[tuple[object, str]]:
    """04_SELF_CHECK minimum rows 6-12: (value, evidence)."""
    import json

    audits = sorted((s.out_dir / "logs").glob("audit_*.json"))
    a = json.loads(audits[-1].read_text()) if audits else {}
    top = max(st.by_country.values(), default=0) / (st.qualified or 1)
    return [
        (n_rows or st.qualified, "out/leads.csv; receipts stats"),
        (sum(st.dupes.values()), "receipts stats --dupes"),
        (f"{a.get('pass_rate', 0):.0%}" if a else "not run", "docs/self_audit.md (receipts audit --n 20 --seed 7)"),
        (f"{a.get('channel_accuracy', 0):.0%}" if a.get("channel_accuracy") is not None else "not run",
         "receipts audit --channels 20"),
        (f"{len(st.by_country)} countries, top {top:.0%}", "out/logs/balance.json; receipts stats --balance"),
        (st.priority_a_full, "out/outreach/priority_a/; receipts stats --priority"),
        ("3 segments x 3 channels x 3 steps", "receipts outreach-check; out/outreach/sequences/"),
    ]


def fill_workbook(db, s, src: Path, dst: Path, leads_csv: Path, cfg_dir: Path | None = None) -> Path:
    if not src.exists():
        raise FileNotFoundError(f"workbook not found at {src}; set workbook.path in settings.yaml")
    wb = load_workbook(src)  # not data_only: formulas are preserved as strings
    writes, _ = plan_writes(db, s, leads_csv, Path(cfg_dir or s.config_dir))
    mismatches = []
    for (sheet, coord), _v in writes.items():
        if sheet not in wb.sheetnames:
            mismatches.append(f"{sheet}!{coord}: sheet missing")
            continue
        cell = wb[sheet][coord]
        if is_formula(cell.value):
            mismatches.append(f"{sheet}!{coord}: holds a formula")
        elif not is_yellow(cell, s):
            mismatches.append(f"{sheet}!{coord}: not a yellow input cell (fill {cell_colour(cell)})")
    if mismatches:
        raise WorkbookMappingError(mismatches)
    for (sheet, coord), v in writes.items():
        wb[sheet][coord].value = v
    dst.parent.mkdir(parents=True, exist_ok=True)
    wb.save(dst)
    recalc_check(dst)
    return dst


def recalc_check(path: Path) -> str:
    """Optional LibreOffice recalculation on a copy, to spot formula errors. The delivered file is untouched."""
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        return "libreoffice not installed; recalculation check skipped"
    tmp = path.parent / "_recalc"
    tmp.mkdir(exist_ok=True)
    shutil.copy(path, tmp / path.name)
    subprocess.run([soffice, "--headless", "--convert-to", "xlsx", "--outdir", str(tmp / "out"), str(tmp / path.name)],
                   capture_output=True, timeout=180)
    return f"recalculated copy in {tmp / 'out'}"
