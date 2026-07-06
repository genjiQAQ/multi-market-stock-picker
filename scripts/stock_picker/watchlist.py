"""Persistent watchlist tracking for repeated picker runs."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import SCHEMA_VERSION, QualityLog, StockRecord
from .reporting import top_global


WATCHLIST_COLUMNS = [
    "watch_status",
    "market",
    "yahoo_symbol",
    "raw_symbol",
    "name",
    "current_rank",
    "previous_rank",
    "rank_change",
    "current_score",
    "previous_score",
    "score_change",
    "rating",
    "result_level",
    "watch_runs",
    "last_seen_at",
]


def generated_at() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def default_watchlist_state_dir() -> Path:
    return Path.home() / "Desktop" / "stock-picker-output" / "_state" / "watchlists"


def resolve_watchlist_state_dir(path: str | None) -> Path:
    return Path(path).expanduser() if path else default_watchlist_state_dir()


def apply_watchlist(
    *,
    records: list[StockRecord],
    output_dir: Path,
    top_n: int,
    watchlist_name: str,
    state_dir: Path,
    lookback_runs: int,
    quality: QualityLog,
) -> dict[str, str]:
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / f"{_safe_name(watchlist_name)}.json"
    previous_state = _read_state(state_path)
    run_at = generated_at()
    top_records = top_global(records, top_n)
    scored_records = [record for record in records if record.is_tradeable and "final_score" in record.scores]
    current_by_key = {_record_key(record): record for record in scored_records}
    top_keys = {_record_key(record) for record in top_records}
    previous_entries = previous_state.get("entries", {}) if isinstance(previous_state.get("entries"), dict) else {}
    entries: dict[str, Any] = {}
    changes: list[dict[str, Any]] = []

    for record in scored_records:
        key = _record_key(record)
        previous = previous_entries.get(key, {})
        if not previous and key not in top_keys:
            continue
        status = _status_for_record(record, previous, key in top_keys)
        _apply_record_watch_fields(record, previous, status, run_at)
        entry = _entry_from_record(record, previous, run_at, status)
        entries[key] = entry
        if status:
            changes.append(_change_row(record, status))

    for key, previous in previous_entries.items():
        if key in current_by_key:
            continue
        missed_runs = int(previous.get("missed_runs") or 0) + 1
        status = "移出观察池" if missed_runs >= lookback_runs or previous.get("last_rating") == "暂不纳入" else "降级观察"
        entry = dict(previous)
        entry["last_status"] = status
        entry["missed_runs"] = missed_runs
        entry["updated_at"] = run_at
        entries[key] = entry
        changes.append(_missing_change_row(entry, status))

    state = {
        "schema_version": SCHEMA_VERSION,
        "watchlist_name": watchlist_name,
        "updated_at": run_at,
        "run_count": int(previous_state.get("run_count") or 0) + 1,
        "lookback_runs": lookback_runs,
        "entries": entries,
    }
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    output_state_path = output_dir / "watchlist_state.json"
    output_changes_path = output_dir / "watchlist_changes.csv"
    output_report_path = output_dir / "watchlist_report.md"
    output_state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    _write_changes_csv(output_changes_path, changes)
    output_report_path.write_text(_build_watchlist_report(watchlist_name, run_at, changes), encoding="utf-8")

    quality.watchlist_state_path = str(state_path)
    quality.watchlist_changes_count = len(changes)
    quality.watchlist_changes = changes
    return {
        "watchlist_state": str(output_state_path),
        "watchlist_changes": str(output_changes_path),
        "watchlist_report": str(output_report_path),
    }


def _read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _safe_name(value: str) -> str:
    text = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in value.strip())
    return text or "default"


def _record_key(record: StockRecord) -> str:
    return f"{record.market}:{record.yahoo_symbol or record.raw_symbol}"


def _status_for_record(record: StockRecord, previous: dict[str, Any], in_top: bool) -> str:
    if not previous and in_top:
        return "新进入"
    if not in_top and previous:
        return "降级观察"
    if not in_top:
        return ""
    if record.rating == "暂不纳入" or record.result_level == "低覆盖率结果":
        return "降级观察"

    score_change = _score_change(record, previous)
    rank_change = _rank_change(record, previous)
    if score_change is not None and score_change <= -8:
        return "降级观察"
    if rank_change is not None and rank_change <= -5:
        return "降级观察"

    previous_watch_runs = int(previous.get("watch_runs") or 0)
    stable_score = score_change is None or score_change >= -3
    stable_rank = rank_change is None or rank_change >= -1
    if previous_watch_runs >= 2 and stable_score and stable_rank:
        return "重点延续"
    return "继续跟踪"


def _apply_record_watch_fields(record: StockRecord, previous: dict[str, Any], status: str, run_at: str) -> None:
    record.watch_status = status
    record.previous_rank = _optional_int(previous.get("last_rank"))
    record.previous_score = _optional_float(previous.get("last_score"))
    record.rank_change = _rank_change(record, previous)
    record.score_change = _score_change(record, previous)
    record.watch_runs = int(previous.get("watch_runs") or 0) + 1 if status in {"新进入", "继续跟踪", "重点延续", "降级观察"} else 0
    record.last_seen_at = run_at


def _entry_from_record(record: StockRecord, previous: dict[str, Any], run_at: str, status: str) -> dict[str, Any]:
    first_seen = previous.get("first_seen_at") or run_at
    watch_runs = int(previous.get("watch_runs") or 0) + 1
    history = list(previous.get("history") or [])[-9:]
    history.append(
        {
            "run_at": run_at,
            "rank": record.rank_global,
            "score": record.scores.get("final_score"),
            "status": status,
            "rating": record.rating,
            "result_level": record.result_level,
        }
    )
    return {
        "market": record.market,
        "raw_symbol": record.raw_symbol,
        "yahoo_symbol": record.yahoo_symbol,
        "name": record.name,
        "first_seen_at": first_seen,
        "last_seen_at": run_at,
        "updated_at": run_at,
        "last_status": status,
        "watch_runs": watch_runs,
        "missed_runs": 0,
        "last_rank": record.rank_global,
        "last_score": record.scores.get("final_score"),
        "last_rating": record.rating,
        "last_result_level": record.result_level,
        "last_industry": record.industry,
        "last_concepts": record.concepts,
        "history": history,
    }


def _change_row(record: StockRecord, status: str) -> dict[str, Any]:
    return {
        "watch_status": status,
        "market": record.market,
        "yahoo_symbol": record.yahoo_symbol,
        "raw_symbol": record.raw_symbol,
        "name": record.name,
        "current_rank": record.rank_global,
        "previous_rank": record.previous_rank,
        "rank_change": record.rank_change,
        "current_score": record.scores.get("final_score"),
        "previous_score": record.previous_score,
        "score_change": record.score_change,
        "rating": record.rating,
        "result_level": record.result_level,
        "watch_runs": record.watch_runs,
        "last_seen_at": record.last_seen_at,
    }


def _missing_change_row(entry: dict[str, Any], status: str) -> dict[str, Any]:
    return {
        "watch_status": status,
        "market": entry.get("market"),
        "yahoo_symbol": entry.get("yahoo_symbol"),
        "raw_symbol": entry.get("raw_symbol"),
        "name": entry.get("name"),
        "current_rank": None,
        "previous_rank": entry.get("last_rank"),
        "rank_change": None,
        "current_score": None,
        "previous_score": entry.get("last_score"),
        "score_change": None,
        "rating": entry.get("last_rating"),
        "result_level": entry.get("last_result_level"),
        "watch_runs": entry.get("watch_runs"),
        "last_seen_at": entry.get("last_seen_at"),
    }


def _write_changes_csv(path: Path, changes: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=WATCHLIST_COLUMNS)
        writer.writeheader()
        for row in changes:
            writer.writerow({column: row.get(column) for column in WATCHLIST_COLUMNS})


def _build_watchlist_report(watchlist_name: str, run_at: str, changes: list[dict[str, Any]]) -> str:
    status_counts: dict[str, int] = {}
    for row in changes:
        status = str(row.get("watch_status") or "未分类")
        status_counts[status] = status_counts.get(status, 0) + 1
    lines = [
        "# 候选跟踪报告",
        "",
        f"- 生成时间：{run_at}",
        f"- Watchlist：{watchlist_name}",
        f"- 变化数量：{len(changes)}",
        f"- 状态统计：{json.dumps(status_counts, ensure_ascii=False)}",
        "",
        "## 变化明细",
        "",
    ]
    if not changes:
        lines.extend(["本次没有可记录的候选变化。", ""])
    for row in changes:
        symbol = row.get("yahoo_symbol") or row.get("raw_symbol") or "UNKNOWN"
        lines.append(
            f"- {row.get('watch_status')}：{row.get('name') or symbol} ({row.get('market')} / {symbol})，"
            f"当前排名 {row.get('current_rank') or '未入选'}，前次排名 {row.get('previous_rank') or '无'}，"
            f"评分变化 {row.get('score_change') if row.get('score_change') is not None else '无'}。"
        )
    lines.extend(["", "本报告仅用于研究辅助，不构成投资建议。"])
    return "\n".join(lines) + "\n"


def _optional_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except Exception:
        return None


def _optional_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _rank_change(record: StockRecord, previous: dict[str, Any]) -> int | None:
    previous_rank = _optional_int(previous.get("last_rank"))
    if previous_rank is None or record.rank_global is None:
        return None
    return previous_rank - record.rank_global


def _score_change(record: StockRecord, previous: dict[str, Any]) -> float | None:
    previous_score = _optional_float(previous.get("last_score"))
    current_score = record.scores.get("final_score")
    if previous_score is None or current_score is None:
        return None
    return round(float(current_score) - previous_score, 2)
