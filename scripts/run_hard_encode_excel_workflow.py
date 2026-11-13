#!/usr/bin/env python3
import argparse
import getpass
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from zipfile import BadZipFile, ZipFile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import openpyxl
import paramiko
import pymysql

import collect_seirios_metrics as metrics


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKBOOK_PATTERN = "4.39"
RECORD_ID_PATTERN = re.compile(r"\d{14}_\d+_rcdctl_\d+")
LAYOUT_PATTERN = re.compile(r"(\d+)\s*\u5206\u5c4f")
ACTIVE_STATES = (3, 4, 5)
CONCLUSION_SHEET_NAME = "\u6d4b\u8bd5\u7ed3\u8bba"
PHASE_CONFIGS = {
    "hard1080p_1_2": {
        "sheet": "\u786c\u7f16-1080p",
        "resolution": "1080p",
        "scenario_prefix": "hard1080p",
        "mode": "hard",
        "rows": [3, 4, 5, 6, 7, 8, 10],
    },
    "hard1080p_all": {
        "sheet": "\u786c\u7f16-1080p",
        "resolution": "1080p",
        "scenario_prefix": "hard1080p",
        "mode": "hard",
        "rows": [3, 4, 5, 6, 7, 8, 10, 12, 14, 16, 18, 20, 22, 24],
    },
    "hard720p_1_2": {
        "sheet": "\u786c\u7f16-720p",
        "resolution": "720p",
        "scenario_prefix": "hard720p",
        "mode": "hard",
        "rows": [3, 4, 5, 6, 7, 8, 10],
    },
    "hard720p_all": {
        "sheet": "\u786c\u7f16-720p",
        "resolution": "720p",
        "scenario_prefix": "hard720p",
        "mode": "hard",
        "rows": [3, 4, 5, 6, 7, 8, 10, 12, 14, 16, 18, 20, 22],
    },
    "soft1080p_all": {
        "sheet": "\u8f6f\u7f16-1080p",
        "resolution": "1080p",
        "scenario_prefix": "soft1080p",
        "mode": "soft",
        "rows": [3, 4, 5, 6, 7, 8, 10, 12, 14, 16, 18, 20, 22, 24],
    },
    "soft720p_all": {
        "sheet": "\u8f6f\u7f16-720p",
        "resolution": "720p",
        "scenario_prefix": "soft720p",
        "mode": "soft",
        "rows": [3, 4, 5, 6, 7, 8, 10, 12, 14, 16, 18, 20, 22],
    },
}
THROUGHPUT_HEADERS = {
    8: "\u7406\u8bba\u5b9e\u65f6\u541e\u5410FPS",
    9: "\u6709\u6548\u7f16\u7801\u541e\u5410FPS",
    10: "\u5b9e\u65f6\u5904\u7406\u6bd4\u4f8b",
    11: "\u7f16\u7801\u65f6\u957f",
    12: "\u4efb\u52a11H\u8017\u65f6\uff08\u6362\u7b97\uff09",
    13: "1H\u5185\u53ef\u5b8c\u6210\u65f6\u957f1H\u7684\u4efb\u52a1\u6570",
    14: "\u6587\u4ef6\u5927\u5c0f",
    15: "\u5907\u6ce8",
}
LEGACY_REPAIR_HEADER = "\u9996\u6b21\u5f02\u5e38\u4efb\u52a1\u6570"
LEGACY_REPAIR_COLUMN_COUNT = 8
BOTTLENECK_REASON_CN = {
    "failed_records": "\u5b58\u5728\u4efb\u52a1\u5931\u8d25\uff0c\u672c\u6b21\u6570\u636e\u4e0d\u80fd\u4f5c\u4e3a\u7a33\u5b9a\u6027\u80fd\u7ed3\u8bba\u3002",
    "incomplete_records": "\u5b58\u5728\u4efb\u52a1\u672a\u5b8c\u6210\uff0c\u672c\u6b21\u6570\u636e\u4e0d\u80fd\u4f5c\u4e3a\u7a33\u5b9a\u6027\u80fd\u7ed3\u8bba\u3002",
    "effective_throughput_below_realtime": "\u6709\u6548\u7f16\u7801\u541e\u5410\u4f4e\u4e8e\u7406\u8bba\u5b9e\u65f6\u9700\u6c42\uff0c\u5f53\u524d\u4e0d\u662f\u5b9e\u65f6\u7f16\u7801\uff0c\u800c\u662f\u6162\u901f\u5904\u7406\u3002",
    "decoder_load_ge_95_percent_for_half_samples": "\u89e3\u7801\u8d1f\u8f7d\u957f\u65f6\u95f4\u63a5\u8fd1100%\uff0c\u74f6\u9888\u4e3b\u8981\u7591\u4f3c\u5728\u89e3\u7801\u4fa7\u3002",
    "decoder_load_reached_100_percent": "\u89e3\u7801\u8d1f\u8f7d\u5df2\u7ecf\u5230100%\uff0c\u9700\u5173\u6ce8\u89e3\u7801\u4fa7\u662f\u5426\u6210\u4e3a\u74f6\u9888\u3002",
    "temperature_ge_90c": "\u52a0\u901f\u5361\u6e29\u5ea6\u8d85\u8fc790C\uff0c\u5b58\u5728\u9ad8\u6e29\u964d\u9891\u6216\u7a33\u5b9a\u6027\u98ce\u9669\u3002",
}


@dataclass
class ExcelScenario:
    row: int
    scenario_id: str
    resolution: str
    mode: str
    record_ids: List[str]
    layout_counts: List[int]

    @property
    def task_count(self) -> int:
        return len(self.record_ids)


def find_default_workbook() -> Path:
    candidates = [
        path for path in PROJECT_ROOT.glob("*.xlsx")
        if DEFAULT_WORKBOOK_PATTERN in path.name
        and "backup" not in path.name.lower()
        and "before_" not in path.name.lower()
        and not path.name.startswith("~$")
    ]
    if not candidates:
        raise SystemExit("No default workbook found; pass --workbook explicitly.")
    return candidates[0]


def load_env_config(path: Optional[str]) -> Dict[str, object]:
    if not path:
        return {}
    config_path = Path(path)
    if not config_path.exists():
        raise SystemExit(f"Environment config not found: {config_path}")
    with config_path.open("r", encoding="utf-8") as file_obj:
        data = json.load(file_obj)
    if not isinstance(data, dict):
        raise SystemExit(f"Environment config must be a JSON object: {config_path}")
    return data


def config_section(config: Dict[str, object], name: str) -> Dict[str, object]:
    value = config.get(name, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SystemExit(f"Environment config section '{name}' must be a JSON object.")
    return value


def config_value(config: Dict[str, object], section: str, key: str, default=None):
    return config_section(config, section).get(key, default)


def config_secret(config: Dict[str, object], section: str, default_env: str, default_value: str = "") -> str:
    section_data = config_section(config, section)
    if section_data.get("password"):
        return str(section_data["password"])
    env_name = str(section_data.get("password_env") or default_env)
    return os.environ.get(env_name, default_value)


def parse_args():
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument("--env-config", help="JSON file for environment-specific DB/SSH/store settings.")
    pre_args, _ = pre_parser.parse_known_args()
    env_config = load_env_config(pre_args.env_config)

    parser = argparse.ArgumentParser(description="Run hard-encode scenarios from the Excel report.")
    parser.add_argument("--env-config", default=pre_args.env_config, help="JSON file for environment-specific DB/SSH/store settings.")
    parser.add_argument("--workbook", default=config_value(env_config, "paths", "workbook"))
    parser.add_argument("--phase", choices=sorted(PHASE_CONFIGS), default="hard1080p_1_2")
    parser.add_argument("--only-row", type=int, help="Run only one Excel row, for example 3.")
    parser.add_argument("--min-row", type=int, help="Run rows greater than or equal to this Excel row.")
    parser.add_argument("--max-row", type=int, help="Run rows less than or equal to this Excel row.")
    parser.add_argument("--dry-run", action="store_true", help="Parse and print actions without DB/SSH/Excel writes.")
    parser.add_argument("--repair-only", action="store_true", help="Only re-encode abnormal record_ids from existing summaries; do not rerun the whole row.")
    parser.add_argument("--target-fps", type=int, default=30)
    parser.add_argument("--record-duration-s", type=int, default=600)
    parser.add_argument("--scenario-timeout-s", type=int, default=7200)
    parser.add_argument("--completion-grace-s", type=int, default=10)
    parser.add_argument("--backup-retention", type=int, default=2, help="Keep only the newest N workbook backups.")
    parser.add_argument("--repair-from-row", type=int, default=16, help="Rows greater than or equal to this value can trigger abnormal file repair.")
    parser.add_argument("--max-reencode-retry", type=int, default=0, help="Retry abnormal output files this many times after the first batch.")
    parser.add_argument("--min-valid-size-mb-1080p", type=float, default=100.0)
    parser.add_argument("--min-valid-size-mb-720p", type=float, default=50.0)
    parser.add_argument("--force-abnormal-record-id", action="append", default=[], help="Force a record_id to be counted as initially abnormal and repaired if present.")
    parser.add_argument("--interval", type=int, default=metrics.DEFAULT_INTERVAL)
    parser.add_argument("--log-scan-interval", type=int, default=metrics.DEFAULT_LOG_SCAN_INTERVAL)
    parser.add_argument("--report-dir", default=str(PROJECT_ROOT / "reports"))

    parser.add_argument("--collector-host", default=str(config_value(env_config, "collector", "host", metrics.DEFAULT_HOST)))
    parser.add_argument("--collector-port", type=int, default=int(config_value(env_config, "collector", "port", metrics.DEFAULT_PORT)))
    parser.add_argument("--collector-user", default=str(config_value(env_config, "collector", "user", metrics.DEFAULT_USER)))
    parser.add_argument("--collector-password", default=config_secret(env_config, "collector", "SEIRIOS_SSH_PASSWORD", metrics.DEFAULT_PASSWORD))

    parser.add_argument("--db-host", default=str(config_value(env_config, "database", "host", "192.168.7.173")))
    parser.add_argument("--db-port", type=int, default=int(config_value(env_config, "database", "port", 22007)))
    parser.add_argument("--db-user", default=str(config_value(env_config, "database", "user", "dbadmin")))
    parser.add_argument("--db-password", default=config_secret(env_config, "database", "SEIRIOS_DB_PASSWORD"))

    parser.add_argument("--record-control-host", default=str(config_value(env_config, "record_control", "host", metrics.DEFAULT_RECORD_CONTROL_HOST)))
    parser.add_argument("--record-control-port", type=int, default=int(config_value(env_config, "record_control", "port", metrics.DEFAULT_RECORD_CONTROL_PORT)))
    parser.add_argument("--record-control-user", default=str(config_value(env_config, "record_control", "user", metrics.DEFAULT_RECORD_CONTROL_USER)))
    parser.add_argument("--record-control-password", default=config_secret(env_config, "record_control", metrics.RECORD_CONTROL_PASSWORD_ENV, metrics.DEFAULT_RECORD_CONTROL_PASSWORD))
    parser.add_argument("--record-control-log-dir", default=str(config_value(env_config, "record_control", "log_dir", metrics.REMOTE_RECORD_CONTROL_LOG)))
    parser.add_argument("--ffprobe-bin", default="ffprobe")
    parser.add_argument("--store-download-url-api", default=str(config_value(env_config, "store", "download_url_api", metrics.DEFAULT_STORE_DOWNLOAD_URL_API)))
    parser.add_argument("--store-app-key", default=str(config_value(env_config, "store", "app_key", os.environ.get(metrics.STORE_APP_KEY_ENV, metrics.DEFAULT_STORE_APP_KEY))))
    parser.add_argument("--store-app-secret", default=str(config_value(env_config, "store", "app_secret", os.environ.get(metrics.STORE_APP_SECRET_ENV, metrics.DEFAULT_STORE_APP_SECRET))))
    args = parser.parse_args()
    if not args.workbook:
        args.workbook = str(find_default_workbook())
    return args


def load_excel_scenarios(workbook_path: Path, phase: str, only_row: Optional[int], min_row: Optional[int], max_row: Optional[int]) -> Tuple[openpyxl.Workbook, object, List[ExcelScenario]]:
    workbook = openpyxl.load_workbook(workbook_path)
    config = PHASE_CONFIGS[phase]
    worksheet = workbook[config["sheet"]]
    if config.get("mode", "hard") == "hard":
        ensure_throughput_columns(worksheet)
    rows = list(config["rows"])
    if only_row is not None:
        rows = [row for row in rows if row == only_row]
        if not rows:
            raise SystemExit(f"--only-row {only_row} is not in phase {phase}.")
    if min_row is not None:
        rows = [row for row in rows if row >= min_row]
        if not rows:
            raise SystemExit(f"--min-row {min_row} leaves no rows in phase {phase}.")
    if max_row is not None:
        rows = [row for row in rows if row <= max_row]
        if not rows:
            raise SystemExit(f"--max-row {max_row} leaves no rows in phase {phase}.")

    scenarios: List[ExcelScenario] = []
    for row in rows:
        text = str(worksheet.cell(row, 2).value or "")
        record_ids = RECORD_ID_PATTERN.findall(text)
        if not record_ids:
            raise ValueError(f"No record_id found in row {row}.")
        layouts = [int(value) for value in LAYOUT_PATTERN.findall(text)]
        if len(layouts) != len(record_ids):
            layouts = layouts[:len(record_ids)] or [1] * len(record_ids)
        scenarios.append(ExcelScenario(
            row=row,
            scenario_id=f"{config['scenario_prefix']}_row{row}_{len(record_ids)}task",
            resolution=str(config["resolution"]),
            mode=str(config.get("mode", "hard")),
            record_ids=record_ids,
            layout_counts=layouts,
        ))
    return workbook, worksheet, scenarios


def ensure_throughput_columns(worksheet) -> None:
    if worksheet.cell(1, 8).value != THROUGHPUT_HEADERS[8]:
        worksheet.insert_cols(9, 2)
    for col, header in THROUGHPUT_HEADERS.items():
        worksheet.cell(1, col).value = header
    remove_legacy_repair_columns(worksheet)


def remove_legacy_repair_columns(worksheet) -> None:
    while worksheet.cell(1, 16).value == LEGACY_REPAIR_HEADER:
        worksheet.delete_cols(16, LEGACY_REPAIR_COLUMN_COUNT)


def placeholders(values: Sequence[object]) -> str:
    return ",".join(["%s"] * len(values))


def connect_db(args):
    return pymysql.connect(
        host=args.db_host,
        port=args.db_port,
        user=args.db_user,
        password=args.db_password,
        charset="utf8mb4",
        autocommit=False,
        connect_timeout=10,
        read_timeout=30,
        write_timeout=30,
    )


def check_no_other_active_tasks(connection, target_record_ids: List[str]) -> None:
    target_sql = placeholders(target_record_ids)
    state_sql = placeholders(ACTIVE_STATES)
    sql = f"""
        SELECT 'record_data' AS source, record_id, state AS state_value
        FROM video_record.record_data
        WHERE state IN ({state_sql}) AND record_id NOT IN ({target_sql})
        UNION ALL
        SELECT 'mission_data' AS source, record_id, status AS state_value
        FROM video_record.mission_data
        WHERE status IN ({state_sql}) AND record_id NOT IN ({target_sql})
        LIMIT 20
    """
    params = list(ACTIVE_STATES) + target_record_ids + list(ACTIVE_STATES) + target_record_ids
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
    if rows:
        raise RuntimeError(f"Active non-target encode tasks found: {rows}")


def reset_records_to_queue(connection, record_ids: List[str]) -> None:
    target_sql = placeholders(record_ids)
    with connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE video_record.mission_data SET status=3 WHERE record_id IN ({target_sql})",
            record_ids,
        )
        cursor.execute(
            f"UPDATE video_record.record_data SET state=3 WHERE record_id IN ({target_sql})",
            record_ids,
        )
    connection.commit()


def file_size_bytes_to_mb(value: object) -> Optional[float]:
    try:
        size_bytes = float(value or 0)
    except (TypeError, ValueError):
        return None
    if size_bytes <= 0:
        return None
    return round(size_bytes / 1024 / 1024, 2)


def query_record_file_sizes_mb(connection, record_ids: List[str]) -> Dict[str, Optional[float]]:
    if not record_ids:
        return {}
    target_sql = placeholders(record_ids)
    sql = f"""
        SELECT record_id, file_size
        FROM video_record.record_data
        WHERE record_id IN ({target_sql})
    """
    result = {record_id: None for record_id in record_ids}
    with connection.cursor() as cursor:
        cursor.execute(sql, record_ids)
        for record_id, file_size in cursor.fetchall():
            result[str(record_id)] = file_size_bytes_to_mb(file_size)
    return result


def restart_record_control(args) -> None:
    client = None
    try:
        last_error = None
        for attempt in range(1, 4):
            try:
                client = paramiko.SSHClient()
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                client.connect(
                    hostname=args.record_control_host,
                    port=args.record_control_port,
                    username=args.record_control_user,
                    password=args.record_control_password,
                    timeout=30,
                    banner_timeout=30,
                    auth_timeout=30,
                )
                break
            except Exception as exc:
                last_error = exc
                if client:
                    client.close()
                    client = None
                time.sleep(min(attempt * 3, 10))
        if client is None:
            raise last_error
        _, stdout, stderr = client.exec_command("docker restart hst-record-control", timeout=120)
        output = stdout.read().decode("utf-8", errors="ignore").strip()
        error = stderr.read().decode("utf-8", errors="ignore").strip()
        if error:
            raise RuntimeError(error)
        if "hst-record-control" not in output:
            raise RuntimeError(f"Unexpected docker restart output: {output}")
    finally:
        if client:
            client.close()


def format_duration(seconds: float) -> str:
    if not seconds:
        return ""
    minutes = seconds / 60
    if minutes < 60:
        return f"{seconds:.3f}s({minutes:.1f}m)"
    return f"{seconds:.3f}s({minutes / 60:.2f}h)"


def format_minutes(seconds: float) -> str:
    if not seconds:
        return ""
    return f"{seconds / 60:.1f}m"


def format_mem_mb(value: float) -> str:
    if value >= 1024:
        return f"{value / 1024:.2f}G"
    return f"{value:.0f}M"


def probe_url_size_mb(url: str, timeout_s: int = 20) -> Optional[float]:
    for method, headers in (("HEAD", {}), ("GET", {"Range": "bytes=0-0"})):
        request = urllib.request.Request(url, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                length = response.headers.get("Content-Length")
                content_range = response.headers.get("Content-Range")
                if content_range and "/" in content_range:
                    length = content_range.rsplit("/", 1)[-1]
                if length:
                    return round(int(length) / 1024 / 1024, 2)
        except (urllib.error.URLError, TimeoutError, ValueError):
            continue
    return None


def hard_size_cell_value(sizes_mb: List[Optional[float]]) -> str:
    if not any(size is not None for size in sizes_mb):
        return ""
    return "\n".join("" if size is None else f"{size:.2f}MB" for size in sizes_mb)


def min_valid_size_mb(args, resolution: str) -> float:
    if str(resolution).lower() == "720p":
        return float(args.min_valid_size_mb_720p)
    return float(args.min_valid_size_mb_1080p)


def abnormal_record_ids(
    record_ids: List[str],
    size_by_record_id: Dict[str, Optional[float]],
    min_size_mb: float,
    forced_record_ids: Sequence[str],
) -> List[str]:
    forced = {str(record_id).strip() for record_id in forced_record_ids if str(record_id).strip()}
    abnormal: List[str] = []
    for record_id in record_ids:
        size_mb = size_by_record_id.get(record_id)
        if record_id in forced or size_mb is None or float(size_mb) < min_size_mb:
            abnormal.append(record_id)
    return abnormal


def bottleneck_reason_cn(summary: Dict[str, object]) -> str:
    reason_text = str(summary.get("bottleneck_reason", "none") or "none")
    reasons = [item.strip() for item in reason_text.split(",") if item.strip() and item.strip() != "none"]
    if not reasons:
        return "\u672a\u53d1\u73b0\u660e\u663e\u74f6\u9888\u3002"
    translated = [BOTTLENECK_REASON_CN.get(reason, reason) for reason in reasons]
    return "\n".join(dict.fromkeys(translated))


def repair_note(summary: Dict[str, object]) -> str:
    initial_abnormal = list(summary.get("initial_abnormal_record_ids") or [])
    final_abnormal = list(summary.get("final_abnormal_record_ids") or [])
    forced = list(summary.get("forced_abnormal_record_ids") or [])
    if not initial_abnormal:
        return "\u9996\u6b21\u5e76\u53d1\u7f16\u7801\u6587\u4ef6\u5927\u5c0f\u6821\u9a8c\u901a\u8fc7\uff0c\u672a\u89e6\u53d1\u8865\u507f\u91cd\u7f16\u3002"
    lines = [
        "\u9996\u6b21\u5e76\u53d1\u7f16\u7801\u5b58\u5728\u8f93\u51fa\u6587\u4ef6\u5f02\u5e38\u504f\u5c0f\uff0c\u7591\u4f3c\u91cd\u7f16\u4e0d\u5b8c\u6574\u3002",
    ]
    if forced:
        lines.append(f"\u5f3a\u5236\u8ba1\u5165\u5f02\u5e38\u5e76\u8865\u507f\u91cd\u7f16: {', '.join(forced)}")
    if final_abnormal:
        lines.append("\u8865\u507f\u540e\u4ecd\u6709\u5f02\u5e38\u6587\u4ef6\uff0c\u672c\u884c\u4e0d\u80fd\u4f5c\u4e3a\u7a33\u5b9a\u6027\u80fd\u7ed3\u8bba\u3002")
    else:
        lines.append("\u8865\u507f\u91cd\u7f16\u540e\u6700\u7ec8\u6587\u4ef6\u5927\u5c0f\u6821\u9a8c\u901a\u8fc7\u3002")
    return "\n".join(lines)


def performance_duration_s(summary: Dict[str, object]) -> float:
    return float(summary.get("final_all_complete_total_duration_s", summary.get("batch_encode_duration_s", 0)) or 0)


def equivalent_60min_duration_s(summary: Dict[str, object]) -> float:
    record_duration_s = float(summary.get("record_duration_s", 0) or 0)
    if not record_duration_s:
        return 0.0
    return performance_duration_s(summary) * 3600 / record_duration_s


def effective_total_fps_for_excel(summary: Dict[str, object]) -> float:
    total_frames = float(summary.get("task_count", 0) or 0) * float(summary.get("record_duration_s", 0) or 0) * float(summary.get("target_fps", 0) or 0)
    duration_s = performance_duration_s(summary)
    return total_frames / duration_s if duration_s else 0.0


def realtime_ratio_for_excel(summary: Dict[str, object]) -> float:
    expected_realtime_fps = float(summary.get("expected_realtime_fps", 0) or 0)
    return effective_total_fps_for_excel(summary) / expected_realtime_fps if expected_realtime_fps else 0.0


def hour_task_capacity_for_excel(summary: Dict[str, object]) -> float:
    target_fps = float(summary.get("target_fps", 0) or 0)
    return effective_total_fps_for_excel(summary) / target_fps if target_fps else 0.0


def repair_brief_note(summary: Dict[str, object]) -> str:
    initial_count = int(summary.get("initial_abnormal_record_count", 0) or 0)
    repair_duration_s = float(summary.get("repair_duration_s", 0) or 0)
    return "\n".join([
        f"\u9996\u6b21\u5f02\u5e38\u4efb\u52a1\u6570\uff1a{initial_count}",
        f"\u8865\u507f\u91cd\u7f16\u8017\u65f6\uff1a{format_duration(repair_duration_s) or '0s'}",
    ])


def update_excel_row(worksheet, row: int, summary: Dict[str, object], sizes_mb: List[Optional[float]]) -> None:
    worksheet.cell(row, 3).value = float(summary.get("avg_record_mix_cpu_percent", 0)) / 100
    worksheet.cell(row, 3).number_format = "0.00%"
    worksheet.cell(row, 4).value = format_mem_mb(float(summary.get("avg_record_mix_mem_mb", 0)))
    worksheet.cell(row, 5).value = float(summary.get("avg_system_cpu_percent", 0)) / 100
    worksheet.cell(row, 5).number_format = "0.00%"
    worksheet.cell(row, 6).value = float(summary.get("avg_system_mem_percent", 0)) / 100
    worksheet.cell(row, 6).number_format = "0.00%"
    monitor_lines = [
        f"\u52a0\u901f\u5361PCIe\u8bfb\u53d6\u5e26\u5bbd:{summary.get('max_pcie_read_mbps', 0)}MB/s",
        f"\u52a0\u901f\u5361PCIe\u5199\u5165\u5e26\u5bbd:{summary.get('max_pcie_write_mbps', 0)}MB/s",
        f"\u52a0\u901f\u5361\u6e29\u5ea6:{summary.get('max_temp_celsius', 0)}C",
        f"\u7f16\u7801\u8d1f\u8f7d:{summary.get('max_enc_load_percent', 0)}%",
        f"\u89e3\u7801\u8d1f\u8f7d:{summary.get('max_dec_load_percent', 0)}%",
    ]
    max_mem_used = float(summary.get("max_mem_used_mb", 0) or 0)
    min_mem_free = float(summary.get("min_mem_free_mb", 0) or 0)
    if max_mem_used or min_mem_free:
        monitor_lines.append(f"\u52a0\u901f\u5361\u5185\u5b58\u4f7f\u7528:{max_mem_used}MB")
        monitor_lines.append(f"\u52a0\u901f\u5361\u5185\u5b58\u5269\u4f59:{min_mem_free}MB")
    worksheet.cell(row, 7).value = "\n".join(monitor_lines)
    worksheet.cell(row, 8).value = float(summary.get("expected_realtime_fps", 0) or 0)
    worksheet.cell(row, 9).value = effective_total_fps_for_excel(summary)
    worksheet.cell(row, 10).value = realtime_ratio_for_excel(summary)
    worksheet.cell(row, 10).number_format = "0.00"
    worksheet.cell(row, 11).value = format_duration(performance_duration_s(summary))
    worksheet.cell(row, 12).value = format_minutes(equivalent_60min_duration_s(summary))
    worksheet.cell(row, 13).value = f"{hour_task_capacity_for_excel(summary):.2f} \u4e2a"
    worksheet.cell(row, 14).value = hard_size_cell_value(sizes_mb)
    notes = [bottleneck_reason_cn(summary)]
    notes.append(repair_brief_note(summary))
    if int(summary.get("initial_abnormal_record_count", 0) or 0):
        notes.append("\u9996\u6b21\u5e76\u53d1\u7f16\u7801\u5b58\u5728\u8f93\u51fa\u6587\u4ef6\u5f02\u5e38\u504f\u5c0f\uff0c\u5df2\u5bf9\u5f02\u5e38\u4efb\u52a1\u5355\u72ec\u8865\u507f\u91cd\u7f16\u3002")
    if int(summary.get("final_abnormal_record_count", 0) or 0):
        notes.append("\u8865\u507f\u540e\u4ecd\u6709\u5f02\u5e38\u6587\u4ef6\uff0c\u672c\u884c\u4e0d\u80fd\u4f5c\u4e3a\u7a33\u5b9a\u6027\u80fd\u7ed3\u8bba\u3002")
    worksheet.cell(row, 15).value = "\n".join(dict.fromkeys(notes))


def update_soft_excel_row(worksheet, row: int, summary: Dict[str, object], sizes_mb: List[Optional[float]]) -> None:
    worksheet.cell(row, 3).value = float(summary.get("avg_record_mix_cpu_percent", 0)) / 100
    worksheet.cell(row, 3).number_format = "0.00%"
    worksheet.cell(row, 4).value = format_mem_mb(float(summary.get("avg_record_mix_mem_mb", 0)))
    worksheet.cell(row, 5).value = float(summary.get("avg_system_cpu_percent", 0)) / 100
    worksheet.cell(row, 5).number_format = "0.00%"
    worksheet.cell(row, 6).value = float(summary.get("avg_system_mem_percent", 0)) / 100
    worksheet.cell(row, 6).number_format = "0.00%"
    worksheet.cell(row, 7).value = format_duration(performance_duration_s(summary))
    worksheet.cell(row, 8).value = format_minutes(equivalent_60min_duration_s(summary))
    worksheet.cell(row, 9).value = f"{hour_task_capacity_for_excel(summary):.2f} \u4e2a"
    worksheet.cell(row, 10).value = hard_size_cell_value(sizes_mb)
    notes = [repair_brief_note(summary)]
    if int(summary.get("initial_abnormal_record_count", 0) or 0):
        notes.append("\u9996\u6b21\u5e76\u53d1\u7f16\u7801\u5b58\u5728\u8f93\u51fa\u6587\u4ef6\u5f02\u5e38\u504f\u5c0f\uff0c\u5df2\u5bf9\u5f02\u5e38\u4efb\u52a1\u5355\u72ec\u8865\u507f\u91cd\u7f16\u3002")
    if int(summary.get("final_abnormal_record_count", 0) or 0):
        notes.append("\u8865\u507f\u540e\u4ecd\u6709\u5f02\u5e38\u6587\u4ef6\uff0c\u672c\u884c\u4e0d\u80fd\u4f5c\u4e3a\u7a33\u5b9a\u6027\u80fd\u7ed3\u8bba\u3002")
    worksheet.cell(row, 11).value = "\n".join(dict.fromkeys(notes))


def load_summary(path: Path) -> Dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def collect_records_once(
    args,
    excel_scenario: ExcelScenario,
    record_ids: List[str],
    scenario_id: str,
    layout_counts: Optional[List[int]] = None,
) -> Dict[str, object]:
    scenario = metrics.Scenario(
        scenario_id=scenario_id,
        start_time=None,
        end_time=None,
        resolution=excel_scenario.resolution,
        target_fps=args.target_fps,
        task_count=len(record_ids),
        layout_counts=layout_counts or [1] * len(record_ids),
        record_duration_s=args.record_duration_s,
    )

    db = connect_db(args)
    collector = metrics.RemoteCollector(args.collector_host, args.collector_port, args.collector_user, args.collector_password)
    record_control_collector = metrics.RemoteCollector(
        args.record_control_host,
        args.record_control_port,
        args.record_control_user,
        args.record_control_password,
    )
    try:
        check_no_other_active_tasks(db, record_ids)
        collector.connect()
        record_control_collector.connect()

        def trigger():
            reset_records_to_queue(db, record_ids)
            restart_record_control(args)

        _, summary_path, _ = metrics.collect_one_scenario(
            collector,
            scenario,
            args.interval,
            args.log_scan_interval,
            Path(args.report_dir),
            dashboard=False,
            continuous=True,
            record_control_collector=record_control_collector,
            status_source_used="record_control",
            record_control_log_dir=args.record_control_log_dir,
            ffprobe_bin=args.ffprobe_bin,
            store_download_url_api=args.store_download_url_api,
            store_app_key=args.store_app_key,
            store_app_secret=args.store_app_secret,
            stop_when_completed=True,
            scenario_timeout_s=args.scenario_timeout_s,
            completion_grace_s=args.completion_grace_s,
            target_record_ids=record_ids,
            trigger_callback=trigger,
        )
        summary = load_summary(summary_path)
        if sorted(summary.get("tracked_record_ids", [])) != sorted(record_ids):
            raise RuntimeError(f"Tracked record IDs do not match Excel row {excel_scenario.row}: {summary.get('tracked_record_ids')}")
        try:
            db.ping(reconnect=True)
        except Exception:
            db.close()
            db = connect_db(args)
        size_by_record_id = query_record_file_sizes_mb(db, record_ids)
        missing_sizes = [record_id for record_id in record_ids if size_by_record_id.get(record_id) is None]
        if missing_sizes:
            print(f"row {excel_scenario.row} missing_file_size: {missing_sizes}")
        summary["file_size_mb_by_record_id"] = size_by_record_id
        summary["file_sizes_mb"] = [size_by_record_id.get(record_id) for record_id in record_ids]
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return summary
    finally:
        try:
            db.close()
        finally:
            collector.close()
            record_control_collector.close()


def run_one_scenario(args, excel_scenario: ExcelScenario, workbook_path: Path) -> Dict[str, object]:
    summary = collect_records_once(
        args,
        excel_scenario,
        excel_scenario.record_ids,
        excel_scenario.scenario_id,
        excel_scenario.layout_counts,
    )
    min_size = min_valid_size_mb(args, excel_scenario.resolution)
    forced_present = [record_id for record_id in args.force_abnormal_record_id if record_id in excel_scenario.record_ids]
    initial_size_by_id = dict(summary.get("file_size_mb_by_record_id") or {})
    initial_abnormal = abnormal_record_ids(excel_scenario.record_ids, initial_size_by_id, min_size, forced_present)
    final_size_by_id = dict(initial_size_by_id)
    repair_summaries: List[Dict[str, object]] = []
    repair_duration_s = 0.0
    current_abnormal = list(initial_abnormal)
    repair_allowed = excel_scenario.row >= int(args.repair_from_row) and int(args.max_reencode_retry) > 0
    attempts = 0
    while repair_allowed and current_abnormal and attempts < int(args.max_reencode_retry):
        attempts += 1
        repair_id = f"{excel_scenario.scenario_id}_repair{attempts}_{len(current_abnormal)}task"
        print(f"row {excel_scenario.row} repair attempt {attempts}: {current_abnormal}")
        repair_summary = collect_records_once(
            args,
            excel_scenario,
            current_abnormal,
            repair_id,
            [1] * len(current_abnormal),
        )
        repair_summaries.append({
            "scenario_id": repair_summary.get("scenario_id"),
            "record_ids": list(current_abnormal),
            "batch_encode_duration_s": repair_summary.get("batch_encode_duration_s", 0),
            "file_size_mb_by_record_id": repair_summary.get("file_size_mb_by_record_id", {}),
        })
        repair_duration_s += float(repair_summary.get("batch_encode_duration_s", 0) or 0)
        final_size_by_id.update(dict(repair_summary.get("file_size_mb_by_record_id") or {}))
        current_abnormal = abnormal_record_ids(excel_scenario.record_ids, final_size_by_id, min_size, [])

    final_abnormal = abnormal_record_ids(excel_scenario.record_ids, final_size_by_id, min_size, [])
    initial_complete_count = excel_scenario.task_count - len(initial_abnormal)
    final_complete_count = excel_scenario.task_count - len(final_abnormal)
    summary["initial_file_size_mb_by_record_id"] = initial_size_by_id
    summary["initial_file_sizes_mb"] = [initial_size_by_id.get(record_id) for record_id in excel_scenario.record_ids]
    summary["initial_abnormal_record_ids"] = initial_abnormal
    summary["initial_abnormal_record_count"] = len(initial_abnormal)
    summary["initial_complete_record_count"] = initial_complete_count
    summary["initial_complete_rate"] = round(initial_complete_count / excel_scenario.task_count, 4) if excel_scenario.task_count else 0
    summary["forced_abnormal_record_ids"] = forced_present
    summary["min_valid_size_mb"] = min_size
    summary["repair_enabled"] = repair_allowed
    summary["repair_attempts"] = attempts
    summary["repair_duration_s"] = round(repair_duration_s, 3)
    summary["repair_summaries"] = repair_summaries
    summary["final_file_size_mb_by_record_id"] = final_size_by_id
    summary["file_size_mb_by_record_id"] = final_size_by_id
    summary["file_sizes_mb"] = [final_size_by_id.get(record_id) for record_id in excel_scenario.record_ids]
    summary["final_abnormal_record_ids"] = final_abnormal
    summary["final_abnormal_record_count"] = len(final_abnormal)
    summary["final_complete_record_count"] = final_complete_count
    summary["final_complete_rate"] = round(final_complete_count / excel_scenario.task_count, 4) if excel_scenario.task_count else 0
    summary["final_all_complete_total_duration_s"] = round(float(summary.get("batch_encode_duration_s", 0) or 0) + repair_duration_s, 3)

    summary_path = Path(args.report_dir) / f"summary_{excel_scenario.scenario_id}.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def summary_path_for(args, excel_scenario: ExcelScenario) -> Path:
    return Path(args.report_dir) / f"summary_{excel_scenario.scenario_id}.json"


def repair_only_scenario(args, excel_scenario: ExcelScenario) -> Dict[str, object]:
    summary_path = summary_path_for(args, excel_scenario)
    if not summary_path.exists():
        raise RuntimeError(f"existing summary not found for repair-only: {summary_path}")

    summary = load_summary(summary_path)
    if sorted(summary.get("tracked_record_ids", [])) != sorted(excel_scenario.record_ids):
        raise RuntimeError(f"Tracked record IDs do not match Excel row {excel_scenario.row}: {summary.get('tracked_record_ids')}")

    min_size = min_valid_size_mb(args, excel_scenario.resolution)
    forced_present = [record_id for record_id in args.force_abnormal_record_id if record_id in excel_scenario.record_ids]
    initial_size_by_id = dict(
        summary.get("initial_file_size_mb_by_record_id")
        or summary.get("file_size_mb_by_record_id")
        or {}
    )
    initial_abnormal = list(summary.get("initial_abnormal_record_ids") or [])
    if not initial_abnormal:
        initial_abnormal = abnormal_record_ids(excel_scenario.record_ids, initial_size_by_id, min_size, forced_present)
    else:
        for record_id in forced_present:
            if record_id not in initial_abnormal:
                initial_abnormal.append(record_id)

    if not initial_size_by_id:
        initial_size_by_id = {record_id: None for record_id in excel_scenario.record_ids}

    current_size_by_id = dict(summary.get("final_file_size_mb_by_record_id") or summary.get("file_size_mb_by_record_id") or initial_size_by_id)
    repair_targets = [record_id for record_id in initial_abnormal if record_id in excel_scenario.record_ids]
    repair_summaries = list(summary.get("repair_summaries") or [])
    repair_duration_s = float(summary.get("repair_duration_s", 0) or 0)
    existing_attempts = int(summary.get("repair_attempts", 0) or 0)
    attempts = 0

    while repair_targets and attempts < int(args.max_reencode_retry):
        attempts += 1
        repair_id = f"{excel_scenario.scenario_id}_repair_only{existing_attempts + attempts}_{len(repair_targets)}task"
        print(f"row {excel_scenario.row} repair-only attempt {attempts}: {repair_targets}")
        repair_summary = collect_records_once(
            args,
            excel_scenario,
            repair_targets,
            repair_id,
            [1] * len(repair_targets),
        )
        repair_summaries.append({
            "scenario_id": repair_summary.get("scenario_id"),
            "record_ids": list(repair_targets),
            "batch_encode_duration_s": repair_summary.get("batch_encode_duration_s", 0),
            "file_size_mb_by_record_id": repair_summary.get("file_size_mb_by_record_id", {}),
            "repair_only": True,
        })
        repair_duration_s += float(repair_summary.get("batch_encode_duration_s", 0) or 0)
        current_size_by_id.update(dict(repair_summary.get("file_size_mb_by_record_id") or {}))
        repair_targets = abnormal_record_ids(excel_scenario.record_ids, current_size_by_id, min_size, [])

    final_abnormal = abnormal_record_ids(excel_scenario.record_ids, current_size_by_id, min_size, [])
    initial_complete_count = excel_scenario.task_count - len(initial_abnormal)
    final_complete_count = excel_scenario.task_count - len(final_abnormal)
    summary["initial_file_size_mb_by_record_id"] = initial_size_by_id
    summary["initial_file_sizes_mb"] = [initial_size_by_id.get(record_id) for record_id in excel_scenario.record_ids]
    summary["initial_abnormal_record_ids"] = initial_abnormal
    summary["initial_abnormal_record_count"] = len(initial_abnormal)
    summary["initial_complete_record_count"] = initial_complete_count
    summary["initial_complete_rate"] = round(initial_complete_count / excel_scenario.task_count, 4) if excel_scenario.task_count else 0
    summary["forced_abnormal_record_ids"] = forced_present
    summary["min_valid_size_mb"] = min_size
    summary["repair_enabled"] = excel_scenario.row >= int(args.repair_from_row) and int(args.max_reencode_retry) > 0
    summary["repair_only"] = True
    summary["repair_attempts"] = existing_attempts + attempts
    summary["repair_duration_s"] = round(repair_duration_s, 3)
    summary["repair_summaries"] = repair_summaries
    summary["final_file_size_mb_by_record_id"] = current_size_by_id
    summary["file_size_mb_by_record_id"] = current_size_by_id
    summary["file_sizes_mb"] = [current_size_by_id.get(record_id) for record_id in excel_scenario.record_ids]
    summary["final_abnormal_record_ids"] = final_abnormal
    summary["final_abnormal_record_count"] = len(final_abnormal)
    summary["final_complete_record_count"] = final_complete_count
    summary["final_complete_rate"] = round(final_complete_count / excel_scenario.task_count, 4) if excel_scenario.task_count else 0
    summary["final_all_complete_total_duration_s"] = round(float(summary.get("batch_encode_duration_s", 0) or 0) + repair_duration_s, 3)

    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def print_dry_run(scenarios: List[ExcelScenario]) -> None:
    for scenario in scenarios:
        target_sql = placeholders(scenario.record_ids)
        print(f"row={scenario.row} scenario={scenario.scenario_id} record_ids={scenario.record_ids}")
        print(f"  UPDATE video_record.mission_data SET status=3 WHERE record_id IN ({target_sql})")
        print(f"  UPDATE video_record.record_data SET state=3 WHERE record_id IN ({target_sql})")
        print(f"  SELECT record_id, file_size FROM video_record.record_data WHERE record_id IN ({target_sql})")
        print("  docker restart hst-record-control")
        print("  fill columns C-K" if scenario.mode == "soft" else "  fill columns C-O")


def print_repair_only_dry_run(args, scenarios: List[ExcelScenario]) -> None:
    for scenario in scenarios:
        if scenario.row < int(args.repair_from_row):
            continue
        summary_path = summary_path_for(args, scenario)
        if not summary_path.exists():
            print(f"row={scenario.row} scenario={scenario.scenario_id} summary_missing={summary_path}")
            continue
        summary = load_summary(summary_path)
        min_size = min_valid_size_mb(args, scenario.resolution)
        forced_present = [record_id for record_id in args.force_abnormal_record_id if record_id in scenario.record_ids]
        size_by_id = dict(
            summary.get("initial_file_size_mb_by_record_id")
            or summary.get("file_size_mb_by_record_id")
            or {}
        )
        abnormal = list(summary.get("initial_abnormal_record_ids") or [])
        if not abnormal:
            abnormal = abnormal_record_ids(scenario.record_ids, size_by_id, min_size, forced_present)
        else:
            for record_id in forced_present:
                if record_id not in abnormal:
                    abnormal.append(record_id)
        target_sql = placeholders(abnormal)
        print(f"row={scenario.row} scenario={scenario.scenario_id} repair_only_record_ids={abnormal}")
        if abnormal:
            print(f"  UPDATE video_record.mission_data SET status=3 WHERE record_id IN ({target_sql})")
            print(f"  UPDATE video_record.record_data SET state=3 WHERE record_id IN ({target_sql})")
            print(f"  SELECT record_id, file_size FROM video_record.record_data WHERE record_id IN ({target_sql})")
            print("  docker restart hst-record-control")
            print("  update final file sizes and remarks")


def backup_workbook(path: Path, keep: int = 2) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = path.with_name(f"{path.stem}.backup_{timestamp}{path.suffix}")
    shutil.copy2(path, backup)
    prune_workbook_backups(path, keep=keep)
    return backup


def list_workbook_backups(path: Path) -> List[Path]:
    prefix = f"{path.stem}."
    return [
        candidate for candidate in path.parent.glob(f"{path.stem}*.xlsx")
        if candidate.name != path.name
        and candidate.name.startswith(prefix)
        and ("backup" in candidate.name.lower() or "before_" in candidate.name.lower())
    ]


def prune_workbook_backups(path: Path, keep: int = 2) -> None:
    if keep < 0:
        return
    backups = sorted(list_workbook_backups(path), key=lambda item: item.stat().st_mtime, reverse=True)
    for backup in backups[keep:]:
        backup.unlink()


def validate_xlsx(path: Path) -> None:
    try:
        with ZipFile(path) as archive:
            archive.getinfo("[Content_Types].xml")
    except (BadZipFile, KeyError) as exc:
        raise RuntimeError(f"Invalid xlsx file after save: {path}") from exc


def save_workbook_atomically(workbook: openpyxl.Workbook, path: Path) -> None:
    temp_path = path.with_name(f".{path.stem}.tmp_{datetime.now().strftime('%Y%m%d_%H%M%S')}{path.suffix}")
    try:
        workbook.save(temp_path)
        validate_xlsx(temp_path)
        shutil.move(str(temp_path), str(path))
    finally:
        if temp_path.exists():
            temp_path.unlink()


def conclusion_text(summaries: List[Dict[str, object]]) -> str:
    ordered = sorted(
        [
            (int(item.get("task_count", 0) or 0), float(item.get("effective_total_fps", 0) or 0))
            for item in summaries
            if int(item.get("task_count", 0) or 0) > 0
        ],
        key=lambda item: item[0],
    )
    if ordered:
        peak_task_count, peak_fps = max(ordered, key=lambda item: item[1])
        later_drops = [(task_count, fps) for task_count, fps in ordered if task_count > peak_task_count and fps < peak_fps]
        if later_drops:
            first_drop_task_count, first_drop_fps = later_drops[0]
            return (
                f"\u6709\u6548\u541e\u5410\u5728{peak_task_count}\u8def\u8fbe\u5230\u6700\u9ad8{peak_fps:.3f}fps\uff0c"
                f"\u540e\u7eed{first_drop_task_count}\u8def\u4e0b\u964d\u5230{first_drop_fps:.3f}fps\uff0c"
                f"\u8bf4\u660e{peak_task_count}-{first_drop_task_count}\u8def\u533a\u95f4\u5df2\u7ecf\u63a5\u8fd1\u6216\u8fbe\u5230\u5f53\u524d\u52a0\u901f\u5361\u5904\u7406\u74f6\u9888\u3002"
            )
    by_task = {int(item.get("task_count", 0) or 0): item for item in summaries}
    task8 = by_task.get(8)
    task18 = by_task.get(18)
    task20 = by_task.get(20)
    if task8 and task18 and task20:
        fps8 = float(task8.get("effective_total_fps", 0) or 0)
        fps18 = float(task18.get("effective_total_fps", 0) or 0)
        fps20 = float(task20.get("effective_total_fps", 0) or 0)
        if fps18 > fps8 and fps20 < fps18:
            return (
                f"18\u8def\u6709\u6548\u541e\u5410{fps18:.3f}fps\uff0c\u9ad8\u4e8e8\u8def{fps8:.3f}fps\uff1b"
                f"20\u8def\u4e0b\u964d\u5230{fps20:.3f}fps\uff0c\u8bf4\u660e18-20\u8def\u533a\u95f4\u5df2\u7ecf\u63a5\u8fd1\u6216\u8fbe\u5230\u5f53\u524d\u52a0\u901f\u5361\u5904\u7406\u74f6\u9888\u3002"
            )
    return "\u8bf7\u5bf9\u6bd4\u5404\u5e76\u53d1\u573a\u666f\u7684\u6709\u6548\u7f16\u7801\u541e\u5410FPS\u548c1\u5c0f\u65f6\u4efb\u52a1\u80fd\u529b\uff0c\u541e\u5410\u4e0d\u518d\u589e\u957f\u6216\u4e0b\u964d\u65f6\u5373\u53ef\u5224\u65ad\u63a5\u8fd1\u74f6\u9888\u3002"


def conclusion_sheet_name(resolution: str) -> str:
    return f"\u6d4b\u8bd5\u7ed3\u8bba-{resolution}"


def write_conclusion_sheet(workbook: openpyxl.Workbook, summaries: List[Dict[str, object]], resolution: str) -> None:
    sheet_name = conclusion_sheet_name(resolution)
    if CONCLUSION_SHEET_NAME in workbook.sheetnames and resolution == "1080p":
        del workbook[CONCLUSION_SHEET_NAME]
    if sheet_name in workbook.sheetnames:
        del workbook[sheet_name]
    worksheet = workbook.create_sheet(sheet_name, 0)
    ordered = sorted(summaries, key=lambda item: (int(item.get("task_count", 0) or 0), str(item.get("scenario_id", ""))))
    worksheet.cell(1, 1).value = f"\u786c\u7f16{resolution}\u6d4b\u8bd5\u7ed3\u8bba"
    worksheet.cell(2, 1).value = "\u603b\u4f53\u7ed3\u8bba"
    worksheet.cell(2, 2).value = conclusion_text(ordered)
    worksheet.cell(3, 1).value = "\u5224\u5b9a\u53e3\u5f84"
    worksheet.cell(3, 2).value = "\u6027\u80fd\u74f6\u9888\u770b\u6709\u6548\u7f16\u7801\u541e\u5410FPS\u30011\u5c0f\u65f6\u4efb\u52a1\u80fd\u529b\u3001\u6279\u6b21\u7f16\u7801\u8017\u65f6\u3001\u677f\u5361\u8d1f\u8f7d\u548c\u6e29\u5ea6\uff1b\u6210\u54c1\u6587\u4ef6FPS\u53ea\u7528\u4e8e\u6821\u9a8c\u8f93\u51fa\u6587\u4ef6\u662f\u5426\u6b63\u5e38\uff0c\u4e0d\u4f5c\u4e3a\u74f6\u9888\u5224\u5b9a\u6307\u6807\u3002"
    headers = [
        "\u4efb\u52a1\u6570",
        "\u7406\u8bba\u5b9e\u65f6\u541e\u5410FPS",
        "\u6709\u6548\u7f16\u7801\u541e\u5410FPS",
        "\u5b9e\u65f6\u5904\u7406\u6bd4\u4f8b",
        "1\u5c0f\u65f6\u4efb\u52a1\u80fd\u529b",
        "\u6700\u9ad8\u89e3\u7801\u8d1f\u8f7d",
        "\u6700\u9ad8\u6e29\u5ea6",
        "\u74f6\u9888\u5224\u65ad",
        "\u539f\u56e0",
    ]
    for col, header in enumerate(headers, start=1):
        worksheet.cell(5, col).value = header
    for row_index, summary in enumerate(ordered, start=6):
        worksheet.cell(row_index, 1).value = int(summary.get("task_count", 0) or 0)
        worksheet.cell(row_index, 2).value = float(summary.get("expected_realtime_fps", 0) or 0)
        worksheet.cell(row_index, 3).value = float(summary.get("effective_total_fps", 0) or 0)
        worksheet.cell(row_index, 4).value = float(summary.get("realtime_ratio", 0) or 0)
        worksheet.cell(row_index, 5).value = float(summary.get("hour_task_capacity", 0) or 0)
        worksheet.cell(row_index, 6).value = float(summary.get("max_dec_load_percent", 0) or 0)
        worksheet.cell(row_index, 7).value = float(summary.get("max_temp_celsius", 0) or 0)
        worksheet.cell(row_index, 8).value = "\u6709\u98ce\u9669" if summary.get("bottleneck_candidate") else "\u672a\u53d1\u73b0\u660e\u663e\u74f6\u9888"
        worksheet.cell(row_index, 9).value = bottleneck_reason_cn(summary)
    widths = {1: 12, 2: 20, 3: 20, 4: 16, 5: 18, 6: 16, 7: 12, 8: 18, 9: 56}
    for col, width in widths.items():
        worksheet.column_dimensions[openpyxl.utils.get_column_letter(col)].width = width


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(line_buffering=True)
    args = parse_args()
    workbook_path = Path(args.workbook)
    workbook, worksheet, scenarios = load_excel_scenarios(workbook_path, args.phase, args.only_row, args.min_row, args.max_row)
    if args.repair_only:
        scenarios = [scenario for scenario in scenarios if scenario.row >= int(args.repair_from_row)]
    if args.dry_run:
        if args.repair_only:
            print_repair_only_dry_run(args, scenarios)
        else:
            print_dry_run(scenarios)
        return 0

    if not args.db_password:
        args.db_password = getpass.getpass("Database password: ")
    if not args.collector_password:
        args.collector_password = getpass.getpass("Collector SSH password: ")
    if not args.record_control_password:
        args.record_control_password = getpass.getpass("Record-control SSH password: ")

    backup = backup_workbook(workbook_path, keep=args.backup_retention)
    print(f"workbook backup: {backup}")

    workbook_dirty = False
    completed_summaries: List[Dict[str, object]] = []
    for scenario in scenarios:
        mode = "repair-only" if args.repair_only else "running"
        print(f"{mode} row {scenario.row}: {scenario.scenario_id} {scenario.record_ids}")
        try:
            if args.repair_only:
                summary = repair_only_scenario(args, scenario)
            else:
                summary = run_one_scenario(args, scenario, workbook_path)
            sizes_mb = list(summary.get("file_sizes_mb") or [])
            while len(sizes_mb) < scenario.task_count:
                sizes_mb.append(None)
            if scenario.mode == "soft":
                update_soft_excel_row(worksheet, scenario.row, summary, sizes_mb)
            else:
                update_excel_row(worksheet, scenario.row, summary, sizes_mb)
            completed_summaries.append(summary)
            workbook_dirty = True
            print(f"row {scenario.row} complete")
        except Exception as exc:
            print(f"row {scenario.row} failed: {exc}", file=sys.stderr)
            continue
        time.sleep(2)
    if workbook_dirty:
        if not args.repair_only and scenarios[0].mode == "hard":
            write_conclusion_sheet(workbook, completed_summaries, scenarios[0].resolution)
        save_workbook_atomically(workbook, workbook_path)
        print(f"workbook saved: {workbook_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
