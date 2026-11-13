#!/usr/bin/env python3
import argparse
import base64
import csv
import getpass
import hashlib
import hmac
import json
import os
import re
import shlex
import signal
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Callable, Deque, Dict, List, Optional, Tuple

import paramiko
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.columns import Columns

DEFAULT_HOST = "192.168.3.58"
DEFAULT_PORT = 22
DEFAULT_USER = "root"
DEFAULT_PASSWORD = ""
DEFAULT_INTERVAL = 1
DEFAULT_LOG_SCAN_INTERVAL = 5
DEFAULT_STATUS_SOURCE = "auto"
DEFAULT_RECORD_CONTROL_HOST = "192.168.7.173"
DEFAULT_RECORD_CONTROL_PORT = 22
DEFAULT_RECORD_CONTROL_USER = "root"
DEFAULT_RECORD_CONTROL_PASSWORD = ""
DEFAULT_STORE_DOWNLOAD_URL_API = "http://192.168.7.173:28080/paas/store/v1/resource/url/download"
RECORD_CONTROL_PASSWORD_ENV = "SEIRIOS_RECORD_CONTROL_PASSWORD"
STORE_APP_KEY_ENV = "SEIRIOS_STORE_APP_KEY"
STORE_APP_SECRET_ENV = "SEIRIOS_STORE_APP_SECRET"
DEFAULT_STORE_APP_KEY = ""
DEFAULT_STORE_APP_SECRET = ""
REMOTE_HWMONITOR = "/hst/emergetech20260107/deploy/hwmonitor"
REMOTE_RECORD_MIX_LOG = "/hst/paas/fsp-record/record-mix/log"
REMOTE_RECORD_CONTROL_LOG = "/hst/paas/fsp-record/record-control/log"
REMOTE_SYSFS_PREFIX = "/sys/devices/virtual/misc/transcoder0"
HWMONITOR_PATTERNS = {
    "pcie_read_mbps": r"pcie_read=\s*(\d+)",
    "pcie_write_mbps": r"pcie_write=\s*(\d+)",
    "temp_celsius": r"temp=\s*(\d+)",
    "enc_load_percent": r"enc=\s*(\d+)",
    "dec_load_percent": r"dec=\s*(\d+)",
    "mem_used_mb": r"mem_used=\s*(\d+)",
    "mem_free_mb": r"mem_free=\s*(\d+)",
}
TASK_PATTERN = re.compile(r"tasks=\s*(\d+)/(\d+)")
INPUT_FPS_PATTERN = re.compile(r"OnMediaDataForVideo, Frames\[(?P<frames>\d+)\], tick offset\[(?P<tick>\d+)\], fps\[(?P<fps>[\d.]+)\], handle: (?P<handle>\d+), context: (?P<context>\d+)")
OUTPUT_FPS_PATTERN = re.compile(r"\[(?P<context>\d+)_(?P<index>\d+)\] HandleVideoMixData.*fps\[(?P<fps>[\d.]+)\]")
STATUS_LINE_TIME_PATTERN = re.compile(r"^\s*(?P<time>\d{2}:\d{2}:\d{2}\.\d{3})\s")
RECORD_CONTROL_STATE_PATTERNS = [
    re.compile(r"AddRecordMission record_id:(?P<record_id>[^,\s]+)\s+mission_id:(?P<mission_id>\d+)\s+state:(?P<state>-?\d+)"),
    re.compile(r"UpdateTaskState record_id:\s*(?P<record_id>[^,\s]+)\s+mission_id:\s*(?P<mission_id>\d+).* state:\s*(?P<state>-?\d+)\b"),
    re.compile(r"UpdateTaskState record_id:\s*(?P<record_id>[^,\s]+)\s+state:\s*(?P<state>-?\d+)\b"),
    re.compile(r"BoradCastStateMsg record:(?P<record_id>[^,\s]+)\s+state:(?P<state>-?\d+)\b"),
]
RECORD_CONTROL_OUTPUT_FILE_PATTERNS = [
    re.compile(
        r"(?:start insert mission sql:|OdbcQuery, )?INSERT INTO mission_data\(record_id,mission_id, file_name, path ,status .*?VALUES\('(?P<record_id>[^']+)'\s*,?\s*'(?P<mission_id>\d+)'\s*,?\s*'(?P<file_name>[^']+)'\s*,?\s*'(?P<path>[^']+)'\s*,?\s*'(?P<state>-?\d+)"
    ),
]
SCENARIO_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
CSV_HEADERS = [
    "timestamp","scenario_id","enc_load_percent","dec_load_percent","temp_celsius","pcie_read_mbps","pcie_write_mbps",
    "mem_used_mb","mem_free_mb",
    "record_mix_cpu_percent","record_mix_mem_mb","system_cpu_percent","system_mem_percent",
    "tasks_active","tasks_total","encoder_input_fps","encoder_output_fps","total_decode_fps","board_fps_total",
    "throttling_status","throttling_time_s","input_fps_source","output_fps_source","status_source",
    "tracked_record_count","completed_record_count","batch_encode_duration_s","last_finished_record_id",
    "actual_output_fps","actual_output_fps_source","ffprobe_completed_count","ffprobe_last_probe_path","ffprobe_last_error"
]
SPARK_CHARS = "._-~=*#@"
STOP = False


@dataclass
class Scenario:
    scenario_id: str
    start_time: Optional[datetime]
    end_time: Optional[datetime]
    resolution: str
    target_fps: int
    task_count: int
    layout_counts: List[int]
    record_duration_s: int = 600
    expected_total_decode_fps: Optional[float] = None
    expected_encoder_input_fps: Optional[float] = None
    expected_encoder_output_fps: Optional[float] = None


def handle_stop(signum, frame):
    global STOP
    STOP = True


class RemoteCollector:
    def __init__(self, host: str, port: int, username: str, password: str):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.client = None
        self._last_record_mix_proc_jiffies: Optional[float] = None
        self._last_host_total_jiffies: Optional[float] = None
        self._last_host_idle_jiffies: Optional[float] = None

    def connect(self):
        last_error = None
        for attempt in range(1, 4):
            try:
                self.client = paramiko.SSHClient()
                self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                self.client.connect(
                    hostname=self.host,
                    port=self.port,
                    username=self.username,
                    password=self.password,
                    timeout=30,
                    banner_timeout=30,
                    auth_timeout=30,
                )
                return
            except Exception as exc:
                last_error = exc
                if self.client:
                    self.client.close()
                    self.client = None
                time.sleep(min(attempt * 3, 10))
        raise last_error

    def close(self):
        if self.client:
            self.client.close()

    def run(self, command: str) -> Tuple[str, str]:
        stdin, stdout, stderr = self.client.exec_command(command, timeout=60)
        return stdout.read().decode('utf-8', errors='ignore'), stderr.read().decode('utf-8', errors='ignore')

    def read_file(self, remote_path: str) -> str:
        sftp = self.client.open_sftp()
        try:
            with sftp.open(remote_path, 'r') as file_obj:
                return file_obj.read().decode('utf-8', errors='ignore')
        finally:
            sftp.close()

    def get_file_size(self, remote_path: str) -> int:
        sftp = self.client.open_sftp()
        try:
            return int(sftp.stat(remote_path).st_size)
        finally:
            sftp.close()

    def read_file_since(self, remote_path: str, offset: int = 0) -> Tuple[str, int]:
        sftp = self.client.open_sftp()
        try:
            size = int(sftp.stat(remote_path).st_size)
            safe_offset = offset if 0 <= offset <= size else 0
            with sftp.open(remote_path, 'r') as file_obj:
                file_obj.seek(safe_offset)
                content = file_obj.read().decode('utf-8', errors='ignore')
            return content, size
        finally:
            sftp.close()

    def get_hwmonitor_metrics(self) -> Dict[str, float]:
        output, _ = self.run(f"{REMOTE_HWMONITOR} -all -s")
        metrics = {}
        for key, pattern in HWMONITOR_PATTERNS.items():
            match = re.search(pattern, output)
            metrics[key] = int(match.group(1)) if match else 0
        match = TASK_PATTERN.search(output)
        if match:
            metrics['tasks_active'] = int(match.group(1))
            metrics['tasks_total'] = int(match.group(2))
        else:
            metrics['tasks_active'] = 0
            metrics['tasks_total'] = 0
        return metrics

    def get_sysfs_metrics(self) -> Dict[str, float]:
        mapping = {
            'enc_load_percent': 'enc_util',
            'dec_load_percent': 'dec_util',
            'temp_celsius': 'temp_sensor_c',
            'pcie_read_mbps': 'pcie_r_bw',
            'pcie_write_mbps': 'pcie_w_bw',
            'tasks_active': 'tasks',
            'throttling_status': 'throttling_status',
            'throttling_time_s': 'throttling_time_s',
        }
        metrics = {}
        for key, name in mapping.items():
            out, _ = self.run(f"cat {REMOTE_SYSFS_PREFIX}/{name} 2>/dev/null || true")
            raw = out.strip()
            if raw.endswith('%'):
                raw = raw[:-1]
            try:
                metrics[key] = float(raw) if raw else 0
            except ValueError:
                metrics[key] = 0
        metrics.setdefault('tasks_total', metrics.get('tasks_active', 0))
        return metrics

    def sample_board_metrics(self) -> Dict[str, float]:
        try:
            hwmonitor = self.get_hwmonitor_metrics()
        except Exception:
            hwmonitor = {}
        sysfs = self.get_sysfs_metrics()
        merged = dict(sysfs)
        merged.update({k: v for k, v in hwmonitor.items() if v not in (None, '')})
        merged.setdefault('throttling_status', 0)
        merged.setdefault('throttling_time_s', 0)
        merged.setdefault('mem_used_mb', 0)
        merged.setdefault('mem_free_mb', 0)
        merged.setdefault('tasks_active', 0)
        merged.setdefault('tasks_total', merged['tasks_active'])
        return merged

    def sample_host_metrics(self) -> Dict[str, float]:
        metrics = {
            'record_mix_cpu_percent': 0.0,
            'record_mix_mem_mb': 0.0,
            'system_cpu_percent': 0.0,
            'system_mem_percent': 0.0,
        }
        proc_cmd = (
            "cpu_count=$(getconf _NPROCESSORS_ONLN 2>/dev/null || nproc 2>/dev/null || echo 1); "
            "read total idle < <(awk '/^cpu / {total=0; for(i=2;i<=NF;i++) total+=$i; print total, $5+$6}' /proc/stat); "
            "proc=0; rss=0; "
            "for pid in $(pgrep -f '/record-mix/bin/record_mix|record_mix' 2>/dev/null | sort -u); do "
            "  [ -r /proc/$pid/stat ] || continue; "
            "  [ -r /proc/$pid/status ] || continue; "
            "  comm=$(awk '{print $2}' /proc/$pid/stat 2>/dev/null); "
            "  case \"$comm\" in '(record_mix)') ;; *) continue ;; esac; "
            "  ticks=$(awk '{print $14+$15}' /proc/$pid/stat 2>/dev/null); "
            "  mem=$(awk '/VmRSS:/ {print $2}' /proc/$pid/status 2>/dev/null); "
            "  proc=$((proc + ${ticks:-0})); rss=$((rss + ${mem:-0})); "
            "done; "
            "awk -v proc=$proc -v total=$total -v idle=$idle -v cpu_count=$cpu_count -v rss=$rss "
            "'BEGIN {printf \"%.0f %.0f %.0f %.0f %.3f\", proc, total, idle, cpu_count, rss/1024}'"
        )
        out, _ = self.run(proc_cmd)
        parts = out.strip().split()
        if len(parts) >= 5:
            try:
                proc_jiffies = float(parts[0])
                total_jiffies = float(parts[1])
                idle_jiffies = float(parts[2])
                cpu_count = float(parts[3]) or 1.0
                metrics['record_mix_mem_mb'] = float(parts[4])
                if (
                    self._last_record_mix_proc_jiffies is not None
                    and self._last_host_total_jiffies is not None
                    and total_jiffies > self._last_host_total_jiffies
                ):
                    proc_delta = proc_jiffies - self._last_record_mix_proc_jiffies
                    total_delta = total_jiffies - self._last_host_total_jiffies
                    metrics['record_mix_cpu_percent'] = max(0.0, round(proc_delta / total_delta * cpu_count * 100, 3))
                if (
                    self._last_host_total_jiffies is not None
                    and self._last_host_idle_jiffies is not None
                    and total_jiffies > self._last_host_total_jiffies
                ):
                    total_delta = total_jiffies - self._last_host_total_jiffies
                    idle_delta = idle_jiffies - self._last_host_idle_jiffies
                    metrics['system_cpu_percent'] = max(0.0, round((total_delta - idle_delta) / total_delta * 100, 3))
                self._last_record_mix_proc_jiffies = proc_jiffies
                self._last_host_total_jiffies = total_jiffies
                self._last_host_idle_jiffies = idle_jiffies
            except ValueError:
                pass

        mem_cmd = "free -m | awk '/Mem:/ {printf \"%.3f\", $3/$2*100}'"
        out, _ = self.run(mem_cmd)
        try:
            metrics['system_mem_percent'] = float(out.strip() or 0)
        except ValueError:
            pass
        return metrics

    def list_latest_log_files(self, date_str: str) -> Dict[str, Optional[str]]:
        folder = f"{REMOTE_RECORD_MIX_LOG}/{date_str}"
        cmd = f"find {folder} -maxdepth 1 -type f \( -name 'record-mix-*.log' -o -name 'VideoMixer-*.log' \) 2>/dev/null | sort"
        out, _ = self.run(cmd)
        files = [line.strip() for line in out.splitlines() if line.strip()]
        result = {'record_mix': None, 'video_mixer': None}
        for path in files:
            name = os.path.basename(path)
            if name.startswith('record-mix-'):
                result['record_mix'] = path
            if name.startswith('VideoMixer-'):
                result['video_mixer'] = path
        return result


def load_manifest(path: Path) -> List[Scenario]:
    raw = json.loads(path.read_text(encoding='utf-8'))
    scenarios = []
    for item in raw['scenarios']:
        scenarios.append(Scenario(
            scenario_id=item['scenario_id'],
            start_time=datetime.strptime(item['start_time'], SCENARIO_TIME_FORMAT) if item.get('start_time') else None,
            end_time=datetime.strptime(item['end_time'], SCENARIO_TIME_FORMAT) if item.get('end_time') else None,
            resolution=item['resolution'],
            target_fps=int(item['target_fps']),
            task_count=int(item['task_count']),
            layout_counts=[int(x) for x in item['layout_counts']],
            record_duration_s=int(item.get('record_duration_s', 600)),
            expected_total_decode_fps=item.get('expected_total_decode_fps'),
            expected_encoder_input_fps=item.get('expected_encoder_input_fps'),
            expected_encoder_output_fps=item.get('expected_encoder_output_fps'),
        ))
    return scenarios


def wait_until(start_time: datetime):
    while True:
        now = datetime.now()
        if now >= start_time:
            return
        remaining = int((start_time - now).total_seconds())
        print(f'waiting for scenario start: {remaining}s')
        time.sleep(min(max(remaining, 1), 5))


def capture_log_offsets(collector: RemoteCollector, latest_files: Dict[str, Optional[str]]) -> Dict[str, int]:
    offsets = {}
    for path in latest_files.values():
        if not path:
            continue
        try:
            offsets[path] = collector.get_file_size(path)
        except Exception:
            offsets[path] = 0
    return offsets


def list_latest_record_control_log_files(collector: RemoteCollector, date_str: str, log_dir: str) -> Dict[str, Optional[str]]:
    folder = f"{log_dir}/{date_str}"
    cmd = f"find {folder} -maxdepth 1 -type f -name 'record-control-*.log' 2>/dev/null | sort"
    out, _ = collector.run(cmd)
    files = [line.strip() for line in out.splitlines() if line.strip()]
    return {'record_control': files[-1] if files else None}


def extract_log_date(remote_path: str) -> str:
    for part in reversed(PurePosixPath(remote_path).parts):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", part):
            return part
    return datetime.now().strftime('%Y-%m-%d')


def parse_status_timestamp(date_str: str, line: str) -> Optional[datetime]:
    match = STATUS_LINE_TIME_PATTERN.match(line)
    if not match:
        return None
    try:
        return datetime.strptime(f"{date_str} {match.group('time')}", "%Y-%m-%d %H:%M:%S.%f")
    except ValueError:
        return None


def parse_record_control_updates(content: str, remote_path: str) -> Dict[str, List[Dict[str, object]]]:
    date_str = extract_log_date(remote_path)
    status_events: List[Dict[str, object]] = []
    file_events: List[Dict[str, object]] = []
    file_id_events: List[Dict[str, object]] = []
    for line in content.splitlines():
        timestamp = parse_status_timestamp(date_str, line)
        if timestamp:
            for pattern in RECORD_CONTROL_STATE_PATTERNS:
                match = pattern.search(line)
                if not match:
                    continue
                state = int(match.group('state'))
                if state not in (-1, 3, 4, 5, 6):
                    break
                mission_id = match.groupdict().get('mission_id')
                status_events.append({
                    'record_id': match.group('record_id').strip(),
                    'mission_id': int(mission_id) if mission_id else None,
                    'state': state,
                    'timestamp': timestamp,
                    'source': 'record_control',
                })
                break
        for pattern in RECORD_CONTROL_OUTPUT_FILE_PATTERNS:
            match = pattern.search(line)
            if not match:
                continue
            file_events.append({
                'record_id': match.group('record_id').strip(),
                'mission_id': int(match.group('mission_id')),
                'output_file_path': str(PurePosixPath(match.group('path')) / match.group('file_name')),
                'timestamp': timestamp,
                'source': 'record_control',
            })
            break
        if 'query record mq_seq:' in line and 'info:' in line:
            _, json_part = line.split('info:', 1)
            try:
                payload = json.loads(json_part.strip())
            except json.JSONDecodeError:
                payload = None
            if payload:
                data = payload.get('data') or {}
                record_id = data.get('record_id')
                record_files = data.get('record_file') or []
                if record_id and record_files:
                    first_file = record_files[0] or {}
                    file_id = first_file.get('file_id')
                    if file_id:
                        file_id_events.append({
                            'record_id': str(record_id).strip(),
                            'file_id': str(file_id).strip(),
                            'timestamp': timestamp,
                            'source': 'record_control_query',
                        })
    return {'status_events': status_events, 'file_events': file_events, 'file_id_events': file_id_events}


def parse_record_control_status(collector: RemoteCollector, latest_files: Dict[str, Optional[str]], log_offsets: Dict[str, int]) -> Dict[str, List[Dict[str, object]]]:
    record_control_log = latest_files.get('record_control')
    if not record_control_log:
        return {'status_events': [], 'file_events': [], 'file_id_events': []}
    start_offset = log_offsets.get(record_control_log, 0)
    try:
        content, end_offset = collector.read_file_since(record_control_log, start_offset)
    except Exception:
        content, end_offset = '', start_offset
    log_offsets[record_control_log] = end_offset
    return parse_record_control_updates(content, record_control_log)


def build_record_state(mission_id: Optional[int] = None) -> Dict[str, object]:
    return {
        'mission_id': mission_id,
        'state_times': {},
        'latest_state': None,
        'output_file_path': None,
        'store_file_id': None,
        'download_url': None,
        'ffprobe_fps': None,
        'ffprobe_last_probe_path': '',
        'ffprobe_last_error': '',
    }


def init_status_tracker(task_count: int, status_source_used: str, target_record_ids: Optional[List[str]] = None) -> Dict[str, object]:
    target_record_ids = [str(record_id).strip() for record_id in (target_record_ids or []) if str(record_id).strip()]
    return {
        'task_count': task_count,
        'status_source_used': status_source_used,
        'target_record_ids': target_record_ids,
        'tracked_record_ids': list(target_record_ids),
        'records': {record_id: build_record_state() for record_id in target_record_ids},
    }


def update_status_tracker(tracker: Dict[str, object], status_events: List[Dict[str, object]], file_events: List[Dict[str, object]]) -> None:
    tracked_record_ids = tracker['tracked_record_ids']
    records = tracker['records']
    task_count = int(tracker['task_count'])
    target_record_ids = set(tracker.get('target_record_ids') or [])
    for event in sorted(status_events, key=lambda item: item['timestamp']):
        record_id = str(event['record_id'])
        if target_record_ids and record_id not in target_record_ids:
            continue
        state = int(event['state'])
        if len(tracked_record_ids) < task_count and state == 3 and record_id not in records:
            tracked_record_ids.append(record_id)
            records[record_id] = build_record_state(event.get('mission_id'))
        record = records.get(record_id)
        if not record:
            continue
        if event.get('mission_id') is not None and record.get('mission_id') is None:
            record['mission_id'] = event.get('mission_id')
        state_times = record['state_times']
        if state not in state_times:
            state_times[state] = event['timestamp']
        record['latest_state'] = state

    for event in file_events:
        record = records.get(str(event['record_id']))
        if not record:
            continue
        if event.get('mission_id') is not None and record.get('mission_id') is None:
            record['mission_id'] = event.get('mission_id')
        if not record.get('output_file_path'):
            record['output_file_path'] = event['output_file_path']


def update_record_file_ids(tracker: Dict[str, object], file_id_events: List[Dict[str, object]]) -> None:
    records = tracker['records']
    for event in file_id_events:
        record = records.get(str(event['record_id']))
        if not record:
            continue
        if not record.get('store_file_id'):
            record['store_file_id'] = event['file_id']

def format_status_time(value: Optional[datetime]) -> str:
    if not value:
        return ''
    return value.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]


def format_duration(value: float) -> str:
    return f"{value:.3f}s" if value else '-'


def base64url_encode(data: bytes) -> bytes:
    return base64.urlsafe_b64encode(data).rstrip(b'=')


def quick_generate_jwt(app_key: str, app_secret: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {"appKey": app_key, "timestamp": int(time.time())}
    encoded_header = base64url_encode(json.dumps(header).encode('utf-8'))
    encoded_payload = base64url_encode(json.dumps(payload).encode('utf-8'))
    signature = hmac.new(app_secret.encode('utf-8'), encoded_header + b'.' + encoded_payload, hashlib.sha256).digest()
    encoded_signature = base64url_encode(signature)
    return (encoded_header + b'.' + encoded_payload + b'.' + encoded_signature).decode('utf-8')


def extract_download_url(payload: object) -> Optional[str]:
    if isinstance(payload, str) and payload.startswith(('http://', 'https://')):
        return payload
    if isinstance(payload, dict):
        for key in ('url', 'downloadUrl', 'resourceUrl', 'fileUrl'):
            value = payload.get(key)
            if isinstance(value, str) and value.startswith(('http://', 'https://')):
                return value
        for nested_key in ('result', 'data'):
            nested = payload.get(nested_key)
            if isinstance(nested, str) and nested.startswith(('http://', 'https://')):
                return nested
            if isinstance(nested, dict):
                for key in ('url', 'downloadUrl', 'resourceUrl', 'fileUrl'):
                    value = nested.get(key)
                    if isinstance(value, str) and value.startswith(('http://', 'https://')):
                        return value
    return None


def fetch_store_download_url(file_id: str, api_url: str, app_key: str, app_secret: str, timeout_s: int = 20) -> Tuple[Optional[str], str]:
    token = quick_generate_jwt(app_key, app_secret)
    request_url = f"{api_url}?{urllib.parse.urlencode({'fileId': file_id})}"
    request = urllib.request.Request(
        request_url,
        headers={
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
        },
        method='GET',
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            body = response.read().decode('utf-8', errors='ignore')
    except urllib.error.HTTPError as exc:
        return None, f'store_http_error:{exc.code}'
    except urllib.error.URLError as exc:
        return None, f'store_url_error:{exc.reason}'
    except Exception as exc:
        return None, f'store_request_error:{exc.__class__.__name__}'
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None, 'store_invalid_json'
    download_url = extract_download_url(payload)
    if not download_url:
        return None, 'store_missing_download_url'
    return download_url, ''


def parse_rate_to_fps(rate: str) -> Optional[float]:
    raw = (rate or '').strip()
    if not raw or raw in ('0/0', 'N/A'):
        return None
    if '/' in raw:
        numerator, denominator = raw.split('/', 1)
        try:
            denominator_value = float(denominator)
            if denominator_value == 0:
                return None
            return float(numerator) / denominator_value
        except ValueError:
            return None
    try:
        return float(raw)
    except ValueError:
        return None


def probe_video_fps(probe_target: str, ffprobe_bin: str) -> Tuple[Optional[float], str]:
    command = [
        ffprobe_bin,
        '-v', 'error',
        '-select_streams', 'v:0',
        '-show_entries', 'stream=avg_frame_rate,r_frame_rate',
        '-of', 'json',
        probe_target,
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='ignore',
            timeout=60,
            check=False,
        )
    except FileNotFoundError:
        return None, 'ffprobe_not_found'
    except subprocess.TimeoutExpired:
        return None, 'ffprobe_timeout'
    except Exception as exc:
        return None, f'ffprobe_exec_error:{exc.__class__.__name__}'
    stderr_text = (completed.stderr or '').strip()
    if completed.returncode != 0:
        return None, stderr_text or f'ffprobe_exit_{completed.returncode}'
    if not completed.stdout.strip():
        return None, stderr_text or 'ffprobe_no_output'
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None, stderr_text or 'ffprobe_invalid_json'
    streams = payload.get('streams') or []
    if not streams:
        return None, stderr_text or 'ffprobe_no_video_stream'
    stream = streams[0]
    for key in ('avg_frame_rate', 'r_frame_rate'):
        fps = parse_rate_to_fps(str(stream.get(key, '')))
        if fps and fps > 0:
            return round(fps, 3), ''
    return None, stderr_text or 'ffprobe_unusable_rate'


def update_completed_file_fps(
    tracker: Dict[str, object],
    collector: Optional[RemoteCollector],
    ffprobe_bin: str,
    store_download_url_api: str,
    store_app_key: Optional[str],
    store_app_secret: Optional[str],
) -> None:
    if not collector:
        return
    for record_id in tracker['tracked_record_ids']:
        record = tracker['records'].get(record_id)
        if not record:
            continue
        latest_state = int(record.get('latest_state') or 0)
        if latest_state < 5:
            continue
        if record.get('ffprobe_fps') is not None:
            continue
        store_file_id = record.get('store_file_id')
        if not store_file_id:
            record['ffprobe_last_error'] = 'missing_store_file_id'
            continue
        if not store_app_key or not store_app_secret:
            record['ffprobe_last_error'] = 'missing_store_credentials'
            continue
        if not record.get('download_url'):
            download_url, download_error = fetch_store_download_url(
                str(store_file_id),
                store_download_url_api,
                store_app_key,
                store_app_secret,
            )
            if download_url:
                record['download_url'] = download_url
            else:
                record['ffprobe_last_error'] = download_error
                continue
        probe_target = record.get('download_url')
        if not probe_target:
            record['ffprobe_last_error'] = 'missing_download_url'
            continue
        record['ffprobe_last_probe_path'] = probe_target
        fps, error = probe_video_fps(str(probe_target), ffprobe_bin)
        if fps is not None:
            record['ffprobe_fps'] = fps
            record['ffprobe_last_error'] = ''
        else:
            record['ffprobe_last_error'] = error


def build_status_metrics(tracker: Dict[str, object], record_duration_s: int = 600) -> Dict[str, object]:
    tracked_record_ids = list(tracker['tracked_record_ids'])
    records = tracker['records']
    task_count = int(tracker['task_count'])
    completed_record_ids: List[str] = []
    failed_record_ids: List[str] = []
    incomplete_record_ids: List[str] = []
    ffprobe_failed_record_ids: List[str] = []
    ffprobe_probe_paths: List[str] = []
    ffprobe_error_samples: List[str] = []
    first_state3_time: Optional[datetime] = None
    last_state6_time: Optional[datetime] = None
    last_finished_record_id = ''
    last_probe_path = ''
    last_probe_error = ''
    actual_output_fps_values: List[float] = []
    record_encode_duration_s_by_id: Dict[str, float] = {}

    for record_id in tracked_record_ids:
        record = records.get(record_id, {})
        state_times = record.get('state_times', {})
        state3_time = state_times.get(3)
        state6_time = state_times.get(6)
        failed_time = state_times.get(-1)
        latest_state = int(record.get('latest_state') or 0)
        ffprobe_fps = record.get('ffprobe_fps')
        probe_path = record.get('ffprobe_last_probe_path') or ''
        probe_error = record.get('ffprobe_last_error') or ''

        if state3_time and (first_state3_time is None or state3_time < first_state3_time):
            first_state3_time = state3_time
        if state6_time:
            completed_record_ids.append(record_id)
            if state3_time:
                record_encode_duration_s_by_id[record_id] = round((state6_time - state3_time).total_seconds(), 3)
            if last_state6_time is None or state6_time > last_state6_time:
                last_state6_time = state6_time
                last_finished_record_id = record_id
        elif failed_time:
            failed_record_ids.append(record_id)
        else:
            incomplete_record_ids.append(record_id)

        if ffprobe_fps is not None:
            actual_output_fps_values.append(float(ffprobe_fps))
        elif latest_state >= 5:
            ffprobe_failed_record_ids.append(record_id)

        if probe_path:
            ffprobe_probe_paths.append(probe_path)
            last_probe_path = probe_path
        if probe_error:
            ffprobe_error_samples.append(probe_error)
            last_probe_error = probe_error

    all_completed = (
        len(tracked_record_ids) == task_count
        and len(completed_record_ids) == task_count
        and not failed_record_ids
    )
    batch_encode_duration_s = 0.0
    if all_completed and first_state3_time and last_state6_time:
        batch_encode_duration_s = round((last_state6_time - first_state3_time).total_seconds(), 3)

    record_encode_durations = list(record_encode_duration_s_by_id.values())
    duration_scale = 3600 / record_duration_s if record_duration_s > 0 else 0
    record_60min_durations = [round(value * duration_scale, 3) for value in record_encode_durations]
    actual_output_fps = round(sum(actual_output_fps_values) / len(actual_output_fps_values), 3) if actual_output_fps_values else 0.0
    return {
        'status_source_used': str(tracker['status_source_used']),
        'tracked_record_count': len(tracked_record_ids),
        'completed_record_count': len(completed_record_ids),
        'batch_encode_duration_s': batch_encode_duration_s,
        'batch_first_queue_time': format_status_time(first_state3_time),
        'batch_last_complete_time': format_status_time(last_state6_time),
        'record_encode_duration_s_by_id': record_encode_duration_s_by_id,
        'min_record_encode_duration_s': safe_min(record_encode_durations),
        'avg_record_encode_duration_s': safe_avg(record_encode_durations),
        'max_record_encode_duration_s': safe_max(record_encode_durations),
        'min_record_60min_equivalent_duration_s': safe_min(record_60min_durations),
        'avg_record_60min_equivalent_duration_s': safe_avg(record_60min_durations),
        'max_record_60min_equivalent_duration_s': safe_max(record_60min_durations),
        'last_finished_record_id': last_finished_record_id,
        'tracked_record_ids': tracked_record_ids,
        'completed_record_ids': completed_record_ids,
        'failed_record_ids': failed_record_ids,
        'incomplete_record_ids': incomplete_record_ids,
        'actual_output_fps': actual_output_fps,
        'actual_output_fps_source': 'ffprobe_store_download' if actual_output_fps_values else 'pending_ffprobe',
        'ffprobe_completed_count': len(actual_output_fps_values),
        'ffprobe_probe_paths': sorted(set(ffprobe_probe_paths)),
        'ffprobe_failed_record_ids': ffprobe_failed_record_ids,
        'ffprobe_error_samples': sorted(set(ffprobe_error_samples))[:10],
        'ffprobe_last_probe_path': last_probe_path,
        'ffprobe_last_error': last_probe_error,
    }

def parse_fps_state(collector: RemoteCollector, latest_files: Dict[str, Optional[str]], log_offsets: Dict[str, int]) -> Dict[str, Dict[str, float]]:
    input_state = {}
    output_state = {}
    record_mix_log = latest_files.get('record_mix')
    if record_mix_log:
        start_offset = log_offsets.get(record_mix_log, 0)
        try:
            content, end_offset = collector.read_file_since(record_mix_log, start_offset)
        except Exception:
            content, end_offset = '', start_offset
        log_offsets[record_mix_log] = end_offset
        for match in INPUT_FPS_PATTERN.finditer(content):
            key = f"{match.group('context')}:{match.group('handle')}"
            input_state[key] = {'fps': float(match.group('fps'))}
        for match in OUTPUT_FPS_PATTERN.finditer(content):
            key = f"{match.group('context')}:{match.group('index')}"
            output_state[key] = {'fps': float(match.group('fps'))}
    return {'input_state': input_state, 'output_state': output_state}


def estimate_metrics(scenario: Scenario, board: Dict[str, float], fps_state: Dict[str, Dict[str, float]]) -> Dict[str, float]:
    output_values = [item['fps'] for item in fps_state['output_state'].values()]
    input_values = [item['fps'] for item in fps_state['input_state'].values()]
    task_divisor = max(scenario.task_count, 1)
    if output_values:
        encoder_output_fps = sum(output_values) / task_divisor
        output_source = 'record_mix_log_avg_per_task'
    else:
        encoder_output_fps = scenario.target_fps
        output_source = 'scenario_fallback_target_per_task'
    if input_values:
        encoder_input_fps = sum(input_values) / task_divisor
        input_source = 'record_mix_log_avg_per_task'
    else:
        encoder_input_fps = scenario.target_fps
        input_source = 'task_count_fallback_target_per_task'
    target_fps_per_task = scenario.target_fps
    board_fps_total = sum(output_values) if output_values else scenario.task_count * scenario.target_fps
    return {
        'encoder_input_fps': round(encoder_input_fps, 3),
        'encoder_output_fps': round(encoder_output_fps, 3),
        'total_decode_fps': round(target_fps_per_task, 3),
        'board_fps_total': round(board_fps_total, 3),
        'input_fps_source': input_source,
        'output_fps_source': output_source,
        'tasks_active': board.get('tasks_active', 0),
        'tasks_total': board.get('tasks_total', 0),
    }


def write_csv(path: Path, rows: List[Dict[str, float]]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8', newline='') as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=CSV_HEADERS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def safe_avg(values: List[float]) -> float:
    return round(sum(values) / len(values), 3) if values else 0.0


def safe_max(values: List[float]) -> float:
    return round(max(values), 3) if values else 0.0


def safe_min(values: List[float]) -> float:
    return round(min(values), 3) if values else 0.0


def safe_div(numerator: float, denominator: float) -> float:
    return round(numerator / denominator, 3) if denominator else 0.0


def build_summary(scenario: Scenario, rows: List[Dict[str, float]], status_metrics: Dict[str, object]) -> Dict[str, object]:
    enc = [float(row['enc_load_percent']) for row in rows]
    dec = [float(row['dec_load_percent']) for row in rows]
    temp = [float(row['temp_celsius']) for row in rows]
    pcie_read = [float(row['pcie_read_mbps']) for row in rows]
    pcie_write = [float(row['pcie_write_mbps']) for row in rows]
    mem_used = [float(row.get('mem_used_mb', 0) or 0) for row in rows]
    mem_free = [float(row.get('mem_free_mb', 0) or 0) for row in rows]
    active_rows = [
        row for row in rows
        if (
            float(row.get('tasks_active', 0) or 0) > 0
            or float(row.get('enc_load_percent', 0) or 0) > 0
            or float(row.get('dec_load_percent', 0) or 0) > 0
        )
    ] or rows
    record_mix_cpu = [float(row.get('record_mix_cpu_percent', 0) or 0) for row in active_rows]
    record_mix_mem = [float(row.get('record_mix_mem_mb', 0) or 0) for row in active_rows]
    system_cpu = [float(row.get('system_cpu_percent', 0) or 0) for row in active_rows]
    system_mem = [float(row.get('system_mem_percent', 0) or 0) for row in active_rows]
    tasks_active = [float(row['tasks_active']) for row in rows]
    actual_output_fps = [float(row['actual_output_fps']) for row in rows if float(row['actual_output_fps']) > 0]
    input_fps = [float(row['encoder_input_fps']) for row in rows]
    output_fps = [float(row['encoder_output_fps']) for row in rows]
    input_sources = sorted({row['input_fps_source'] for row in rows})
    output_sources = sorted({row['output_fps_source'] for row in rows})
    stable_points = [1 for row in rows if float(row['actual_output_fps']) >= scenario.target_fps * 0.95]
    batch_encode_duration_s = float(status_metrics['batch_encode_duration_s'] or 0)
    record_duration_s = int(scenario.record_duration_s or 0)
    expected_realtime_fps = scenario.task_count * scenario.target_fps
    expected_total_frames = scenario.task_count * record_duration_s * scenario.target_fps
    effective_total_fps = safe_div(expected_total_frames, batch_encode_duration_s)
    effective_per_task_fps = safe_div(record_duration_s * scenario.target_fps, batch_encode_duration_s)
    realtime_ratio = safe_div(effective_total_fps, expected_realtime_fps)
    hour_task_capacity = safe_div(effective_total_fps, scenario.target_fps)
    equivalent_60min_batch_duration_s = round(batch_encode_duration_s * 3600 / record_duration_s, 3) if record_duration_s else 0.0
    runtime_fps_reliable = (
        input_sources == ['record_mix_log_avg_per_task']
        and output_sources == ['record_mix_log_avg_per_task']
    )
    dec_load_ge_95_ratio = round(sum(1 for value in dec if value >= 95) / len(dec), 4) if dec else 0.0
    bottleneck_reasons = []
    if status_metrics['failed_record_ids']:
        bottleneck_reasons.append('failed_records')
    if status_metrics['incomplete_record_ids']:
        bottleneck_reasons.append('incomplete_records')
    if batch_encode_duration_s and realtime_ratio < 0.8:
        bottleneck_reasons.append('effective_throughput_below_realtime')
    if dec_load_ge_95_ratio >= 0.5:
        bottleneck_reasons.append('decoder_load_ge_95_percent_for_half_samples')
    elif safe_max(dec) >= 100:
        bottleneck_reasons.append('decoder_load_reached_100_percent')
    if safe_max(temp) >= 90:
        bottleneck_reasons.append('temperature_ge_90c')
    bottleneck_candidate = bool(bottleneck_reasons)
    return {
        'scenario_id': scenario.scenario_id,
        'resolution': scenario.resolution,
        'target_fps': scenario.target_fps,
        'task_count': scenario.task_count,
        'layout_counts': scenario.layout_counts,
        'record_duration_s': record_duration_s,
        'samples': len(rows),
        'avg_enc_load_percent': safe_avg(enc),
        'max_enc_load_percent': safe_max(enc),
        'avg_dec_load_percent': safe_avg(dec),
        'max_dec_load_percent': safe_max(dec),
        'dec_load_ge_95_ratio': dec_load_ge_95_ratio,
        'avg_temp_celsius': safe_avg(temp),
        'max_temp_celsius': safe_max(temp),
        'avg_pcie_read_mbps': safe_avg(pcie_read),
        'max_pcie_read_mbps': safe_max(pcie_read),
        'avg_pcie_write_mbps': safe_avg(pcie_write),
        'max_pcie_write_mbps': safe_max(pcie_write),
        'avg_mem_used_mb': safe_avg(mem_used),
        'max_mem_used_mb': safe_max(mem_used),
        'avg_mem_free_mb': safe_avg(mem_free),
        'min_mem_free_mb': safe_min(mem_free),
        'avg_record_mix_cpu_percent': safe_avg(record_mix_cpu),
        'max_record_mix_cpu_percent': safe_max(record_mix_cpu),
        'avg_record_mix_mem_mb': safe_avg(record_mix_mem),
        'max_record_mix_mem_mb': safe_max(record_mix_mem),
        'avg_system_cpu_percent': safe_avg(system_cpu),
        'max_system_cpu_percent': safe_max(system_cpu),
        'avg_system_mem_percent': safe_avg(system_mem),
        'max_system_mem_percent': safe_max(system_mem),
        'avg_tasks_active': safe_avg(tasks_active),
        'max_tasks_active': safe_max(tasks_active),
        'avg_actual_output_fps_per_probed_task': safe_avg(actual_output_fps),
        'min_actual_output_fps_per_probed_task': safe_min(actual_output_fps),
        'max_actual_output_fps_per_probed_task': safe_max(actual_output_fps),
        'file_fps_by_ffprobe': float(status_metrics['actual_output_fps']),
        'stable_actual_output_fps_ratio': round((sum(stable_points) / len(rows)), 4) if rows else 0,
        'debug_encoder_input_fps_per_task': safe_avg(input_fps),
        'debug_encoder_output_fps_per_task': safe_avg(output_fps),
        'debug_encoder_input_fps_sources': ','.join(input_sources),
        'debug_encoder_output_fps_sources': ','.join(output_sources),
        'runtime_fps_reliable': runtime_fps_reliable,
        'runtime_fps_note': 'Runtime FPS is reliable only when input and output sources are record_mix_log_avg_per_task; fallback values are configured targets, not measured throughput.',
        'status_source_used': status_metrics['status_source_used'],
        'tracked_record_count': status_metrics['tracked_record_count'],
        'completed_record_count': status_metrics['completed_record_count'],
        'ffprobe_completed_count': status_metrics['ffprobe_completed_count'],
        'actual_output_fps_source': status_metrics['actual_output_fps_source'],
        'batch_encode_duration_s': batch_encode_duration_s,
        'expected_realtime_fps': expected_realtime_fps,
        'effective_total_fps': effective_total_fps,
        'effective_per_task_fps': effective_per_task_fps,
        'realtime_ratio': realtime_ratio,
        'hour_task_capacity': hour_task_capacity,
        'equivalent_60min_batch_duration_s': equivalent_60min_batch_duration_s,
        'batch_first_queue_time': status_metrics['batch_first_queue_time'],
        'batch_last_complete_time': status_metrics['batch_last_complete_time'],
        'record_encode_duration_s_by_id': status_metrics['record_encode_duration_s_by_id'],
        'min_record_encode_duration_s': status_metrics['min_record_encode_duration_s'],
        'avg_record_encode_duration_s': status_metrics['avg_record_encode_duration_s'],
        'max_record_encode_duration_s': status_metrics['max_record_encode_duration_s'],
        'min_record_60min_equivalent_duration_s': status_metrics['min_record_60min_equivalent_duration_s'],
        'avg_record_60min_equivalent_duration_s': status_metrics['avg_record_60min_equivalent_duration_s'],
        'max_record_60min_equivalent_duration_s': status_metrics['max_record_60min_equivalent_duration_s'],
        'bottleneck_candidate': bottleneck_candidate,
        'bottleneck_reason': ','.join(bottleneck_reasons) if bottleneck_reasons else 'none',
        'last_finished_record_id': status_metrics['last_finished_record_id'],
        'tracked_record_ids': status_metrics['tracked_record_ids'],
        'completed_record_ids': status_metrics['completed_record_ids'],
        'failed_record_ids': status_metrics['failed_record_ids'],
        'incomplete_record_ids': status_metrics['incomplete_record_ids'],
        'ffprobe_probe_paths': status_metrics['ffprobe_probe_paths'],
        'ffprobe_failed_record_ids': status_metrics['ffprobe_failed_record_ids'],
        'ffprobe_error_samples': status_metrics['ffprobe_error_samples'],
        'ffprobe_last_probe_path': status_metrics['ffprobe_last_probe_path'],
        'ffprobe_last_error': status_metrics['ffprobe_last_error'],
        'task_count_note': 'tasks_active/tasks_total are board-side counters from hwmonitor/sysfs, not a 1:1 mapping to business recording jobs.',
        'actual_fps_note': 'File FPS from ffprobe validates output file correctness only; it is not a hardware bottleneck metric.',
        'file_fps_by_ffprobe_note': 'Use file_fps_by_ffprobe to confirm the output file frame rate, not to judge encoding throughput.',
        'throughput_note': 'Hardware bottlenecks should be judged with effective_total_fps, hour_task_capacity, encode duration, board load, temperature, and task failures.',
        'debug_fps_note': 'Debug log FPS values are used for runtime diagnosis only when runtime_fps_reliable is true.',
        'batch_encode_duration_note': 'Measured from the first tracked task entering state 3 to the last tracked task entering state 6.',
        'status_note': 'Tracked tasks are the first task_count unique record_id values entering state 3 after collector start.',
        'target_fps_note': 'target_fps is a configured expectation only and is not shown as a dashboard metric.',
        'task_success_count': len(status_metrics['completed_record_ids']),
        'task_failure_count': len(status_metrics['failed_record_ids']) + len(status_metrics['incomplete_record_ids']),
    }

def write_summary_json(path: Path, summary: Dict[str, float]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')


def write_summary_markdown(path: Path, summary: Dict[str, float]):
    lines = [
        '# Summary',
        '',
        '## Bottleneck Analysis',
        '',
        f"- Batch encode duration: {format_duration(float(summary.get('batch_encode_duration_s', 0) or 0))}",
        f"- Effective total FPS: {summary.get('effective_total_fps', 0)}",
        f"- Effective per-task FPS: {summary.get('effective_per_task_fps', 0)}",
        f"- 60-minute task capacity per hour: {summary.get('hour_task_capacity', 0)}",
        f"- Equivalent 60-minute batch duration: {format_duration(float(summary.get('equivalent_60min_batch_duration_s', 0) or 0))}",
        f"- File FPS by ffprobe: {summary.get('file_fps_by_ffprobe', 0)}",
        f"- Accelerator memory used max: {summary.get('max_mem_used_mb', 0)}MB",
        f"- Accelerator memory free min: {summary.get('min_mem_free_mb', 0)}MB",
        f"- Runtime FPS reliable: {summary.get('runtime_fps_reliable', False)}",
        f"- Bottleneck candidate: {summary.get('bottleneck_candidate', False)}",
        f"- Bottleneck reason: {summary.get('bottleneck_reason', 'none')}",
        '',
        '## All Metrics',
        '',
        '| Key | Value |',
        '|---|---|',
    ]
    for key, value in summary.items():
        lines.append(f'| {key} | {value} |')
    path.write_text('\n'.join(lines), encoding='utf-8')


def sparkline(values: Deque[float]) -> str:
    if not values:
        return '-'
    arr = list(values)
    mn = min(arr)
    mx = max(arr)
    if mx == mn:
        return SPARK_CHARS[0] * len(arr)
    chars = []
    for value in arr:
        idx = int((value - mn) / (mx - mn) * (len(SPARK_CHARS) - 1))
        chars.append(SPARK_CHARS[idx])
    return ''.join(chars)


def metric_panel(title: str, unit: str, values: Deque[float], color: str) -> Panel:
    current = values[-1] if values else 0
    avg = safe_avg(list(values)) if values else 0
    max_v = safe_max(list(values)) if values else 0
    txt = Text()
    txt.append(f'current: {current}{unit}\n', style=f'bold {color}')
    txt.append(f'avg:     {avg}{unit}\n')
    txt.append(f'max:     {max_v}{unit}\n')
    txt.append(f'{sparkline(values)}', style=color)
    return Panel(txt, title=title, border_style=color)


def dashboard_layout(history: Dict[str, Deque[float]], latest_row: Dict[str, float], scenario: Scenario):
    top = Table.grid(expand=True)
    top.add_column()
    summary = Text()
    summary.append(f'scenario: {scenario.scenario_id}\n', style='bold cyan')
    summary.append(f'resolution: {scenario.resolution}  target_fps: {scenario.target_fps}  task_count: {scenario.task_count}\n')
    summary.append(
        f'tasks_active: {latest_row.get("tasks_active", 0)}  tasks_total: {latest_row.get("tasks_total", 0)}  temp: {latest_row.get("temp_celsius", 0)}C\n'
    )
    summary.append(
        f'pcie_read: {latest_row.get("pcie_read_mbps", 0)}MB/s  pcie_write: {latest_row.get("pcie_write_mbps", 0)}MB/s\n'
    )
    summary.append(
        f'tracked: {latest_row.get("tracked_record_count", 0)}/{scenario.task_count}  completed: {latest_row.get("completed_record_count", 0)}/{scenario.task_count}\n'
    )
    summary.append(
        f'encode_duration: {format_duration(float(latest_row.get("batch_encode_duration_s", 0) or 0))}  last_finished: {latest_row.get("last_finished_record_id") or "-"}\n',
        style='dim',
    )
    summary.append(
        f'ffprobe_completed: {int(latest_row.get("ffprobe_completed_count", 0) or 0)}/{scenario.task_count}  actual_file_fps: {latest_row.get("actual_output_fps", 0)}',
        style='dim',
    )
    top.add_row(Panel(summary, title='Seirios Monitor', border_style='cyan'))
    panels = Columns([
        metric_panel('Encoder Load', '%', history['enc_load_percent'], 'green'),
        metric_panel('Decoder Load', '%', history['dec_load_percent'], 'yellow'),
        metric_panel('PCIe Read MB/s', '', history['pcie_read_mbps'], 'white'),
        metric_panel('PCIe Write MB/s', '', history['pcie_write_mbps'], 'bright_black'),
        metric_panel('Actual File FPS / Task', '', history['actual_output_fps'], 'bright_cyan'),
    ], expand=True)
    return Table.grid(expand=True).add_row(top) if False else Columns([top, panels], expand=True)

def init_history() -> Dict[str, Deque[float]]:
    return {
        'enc_load_percent': deque(maxlen=60),
        'dec_load_percent': deque(maxlen=60),
        'pcie_read_mbps': deque(maxlen=60),
        'pcie_write_mbps': deque(maxlen=60),
        'actual_output_fps': deque(maxlen=60),
    }

def collect_one_scenario(
    collector: RemoteCollector,
    scenario: Scenario,
    interval: int,
    log_scan_interval: int,
    report_dir: Path,
    dashboard: bool = False,
    continuous: bool = False,
    record_control_collector: Optional[RemoteCollector] = None,
    status_source_used: str = 'disabled',
    record_control_log_dir: str = REMOTE_RECORD_CONTROL_LOG,
    ffprobe_bin: str = 'ffprobe',
    store_download_url_api: str = DEFAULT_STORE_DOWNLOAD_URL_API,
    store_app_key: Optional[str] = None,
    store_app_secret: Optional[str] = None,
    stop_when_completed: bool = False,
    scenario_timeout_s: int = 0,
    completion_grace_s: int = 0,
    target_record_ids: Optional[List[str]] = None,
    trigger_callback: Optional[Callable[[], None]] = None,
):
    now = datetime.now()
    if not continuous:
        if scenario.end_time and now > scenario.end_time:
            raise ValueError(f'scenario window already ended: {scenario.scenario_id}')
        if scenario.start_time and now < scenario.start_time:
            wait_until(scenario.start_time)
    metrics_rows = []
    latest_files = collector.list_latest_log_files(datetime.now().strftime('%Y-%m-%d'))
    log_offsets = capture_log_offsets(collector, latest_files)
    record_control_files: Dict[str, Optional[str]] = {}
    record_control_offsets: Dict[str, int] = {}
    if record_control_collector:
        record_control_files = list_latest_record_control_log_files(record_control_collector, datetime.now().strftime('%Y-%m-%d'), record_control_log_dir)
        record_control_offsets = capture_log_offsets(record_control_collector, record_control_files)
    last_log_scan = 0.0
    last_status_log_scan = 0.0
    history = init_history()
    status_tracker = init_status_tracker(scenario.task_count, status_source_used, target_record_ids)
    status_metrics = build_status_metrics(status_tracker, scenario.record_duration_s)
    if trigger_callback:
        trigger_callback()
    console = Console()
    live = Live(console=console, refresh_per_second=2, transient=False) if dashboard else None
    if live:
        live.start()
    started_at = time.time()
    completion_deadline: Optional[float] = None
    try:
        while True:
            now = datetime.now()
            if not continuous and scenario.end_time and now > scenario.end_time:
                break
            if STOP:
                break
            if scenario_timeout_s and time.time() - started_at >= scenario_timeout_s:
                break
            board = collector.sample_board_metrics()
            host = collector.sample_host_metrics()
            if time.time() - last_log_scan >= log_scan_interval or not latest_files:
                latest_files = collector.list_latest_log_files(now.strftime('%Y-%m-%d'))
                for path in latest_files.values():
                    if path and path not in log_offsets:
                        log_offsets[path] = 0
                last_log_scan = time.time()
            fps_state = parse_fps_state(collector, latest_files, log_offsets)
            calculated = estimate_metrics(scenario, board, fps_state)

            if record_control_collector:
                if time.time() - last_status_log_scan >= log_scan_interval or not record_control_files:
                    record_control_files = list_latest_record_control_log_files(record_control_collector, now.strftime('%Y-%m-%d'), record_control_log_dir)
                    for path in record_control_files.values():
                        if path and path not in record_control_offsets:
                            record_control_offsets[path] = 0
                    last_status_log_scan = time.time()
                status_updates = parse_record_control_status(record_control_collector, record_control_files, record_control_offsets)
                update_status_tracker(status_tracker, status_updates['status_events'], status_updates['file_events'])
                update_record_file_ids(status_tracker, status_updates.get('file_id_events', []))
                update_completed_file_fps(
                    status_tracker,
                    record_control_collector,
                    ffprobe_bin,
                    store_download_url_api,
                    store_app_key,
                    store_app_secret,
                )
                status_metrics = build_status_metrics(status_tracker, scenario.record_duration_s)

            row = {
                'timestamp': now.strftime(SCENARIO_TIME_FORMAT),
                'scenario_id': scenario.scenario_id,
                'enc_load_percent': board.get('enc_load_percent', 0),
                'dec_load_percent': board.get('dec_load_percent', 0),
                'temp_celsius': board.get('temp_celsius', 0),
                'pcie_read_mbps': board.get('pcie_read_mbps', 0),
                'pcie_write_mbps': board.get('pcie_write_mbps', 0),
                'mem_used_mb': board.get('mem_used_mb', 0),
                'mem_free_mb': board.get('mem_free_mb', 0),
                'record_mix_cpu_percent': host.get('record_mix_cpu_percent', 0),
                'record_mix_mem_mb': host.get('record_mix_mem_mb', 0),
                'system_cpu_percent': host.get('system_cpu_percent', 0),
                'system_mem_percent': host.get('system_mem_percent', 0),
                'tasks_active': calculated['tasks_active'],
                'tasks_total': calculated['tasks_total'],
                'encoder_input_fps': calculated['encoder_input_fps'],
                'encoder_output_fps': calculated['encoder_output_fps'],
                'total_decode_fps': calculated['total_decode_fps'],
                'board_fps_total': calculated['board_fps_total'],
                'throttling_status': board.get('throttling_status', 0),
                'throttling_time_s': board.get('throttling_time_s', 0),
                'input_fps_source': calculated['input_fps_source'],
                'output_fps_source': calculated['output_fps_source'],
                'status_source': status_metrics['status_source_used'],
                'tracked_record_count': status_metrics['tracked_record_count'],
                'completed_record_count': status_metrics['completed_record_count'],
                'batch_encode_duration_s': status_metrics['batch_encode_duration_s'],
                'last_finished_record_id': status_metrics['last_finished_record_id'],
                'actual_output_fps': status_metrics['actual_output_fps'],
                'actual_output_fps_source': status_metrics['actual_output_fps_source'],
                'ffprobe_completed_count': status_metrics['ffprobe_completed_count'],
                'ffprobe_last_probe_path': status_metrics['ffprobe_last_probe_path'],
                'ffprobe_last_error': status_metrics['ffprobe_last_error'],
            }
            metrics_rows.append(row)
            for key in history:
                history[key].append(float(row[key]))
            if dashboard and live:
                live.update(Columns([
                    Panel(
                        "scenario: {scenario}\nresolution: {resolution}\n"
                        "tasks_active: {tasks_active}/{tasks_total}\n"
                        "temp: {temp}C\n"
                        "pcie_read: {pcie_read}MB/s  pcie_write: {pcie_write}MB/s\n"
                        "tracked: {tracked}/{task_count}  completed: {completed}/{task_count}\n"
                        "encode_duration: {encode_duration}  last_finished: {last_finished}\n"
                        "ffprobe_completed: {ffprobe_completed}/{task_count}  actual_file_fps: {actual_file_fps}".format(
                            scenario=scenario.scenario_id,
                            resolution=scenario.resolution,
                            tasks_active=row['tasks_active'],
                            tasks_total=row['tasks_total'],
                            temp=row['temp_celsius'],
                            pcie_read=row['pcie_read_mbps'],
                            pcie_write=row['pcie_write_mbps'],
                            tracked=row['tracked_record_count'],
                            completed=row['completed_record_count'],
                            task_count=scenario.task_count,
                            encode_duration=format_duration(float(row['batch_encode_duration_s'])),
                            last_finished=row['last_finished_record_id'] or '-',
                            ffprobe_completed=row['ffprobe_completed_count'],
                            actual_file_fps=row['actual_output_fps'],
                        ),
                        title='Seirios Monitor',
                        border_style='cyan',
                    ),
                    metric_panel('Encoder Load', '%', history['enc_load_percent'], 'green'),
                    metric_panel('Decoder Load', '%', history['dec_load_percent'], 'yellow'),
                    metric_panel('PCIe Read MB/s', '', history['pcie_read_mbps'], 'white'),
                    metric_panel('PCIe Write MB/s', '', history['pcie_write_mbps'], 'bright_black'),
                    metric_panel('Actual File FPS / Task', '', history['actual_output_fps'], 'bright_cyan'),
                ], expand=True))
            else:
                print(json.dumps(row, ensure_ascii=False))
            if stop_when_completed:
                all_done = (
                    int(status_metrics['tracked_record_count']) == scenario.task_count
                    and int(status_metrics['completed_record_count']) == scenario.task_count
                )
                has_failed = bool(status_metrics['failed_record_ids'])
                if all_done or has_failed:
                    if completion_deadline is None:
                        completion_deadline = time.time() + max(completion_grace_s, 0)
                    elif time.time() >= completion_deadline:
                        break
            time.sleep(interval)
    finally:
        if live:
            live.stop()
    csv_path = report_dir / f'metrics_{scenario.scenario_id}.csv'
    summary_json_path = report_dir / f'summary_{scenario.scenario_id}.json'
    summary_md_path = report_dir / f'summary_{scenario.scenario_id}.md'
    write_csv(csv_path, metrics_rows)
    summary = build_summary(scenario, metrics_rows, status_metrics)
    write_summary_json(summary_json_path, summary)
    write_summary_markdown(summary_md_path, summary)
    return csv_path, summary_json_path, summary_md_path

def parse_args():
    parser = argparse.ArgumentParser(description='Collect Seirios board metrics for record_mix scenarios.')
    parser.add_argument('--host', default=DEFAULT_HOST)
    parser.add_argument('--port', type=int, default=DEFAULT_PORT)
    parser.add_argument('--username', default=DEFAULT_USER)
    parser.add_argument('--password', default=os.environ.get('SEIRIOS_SSH_PASSWORD', DEFAULT_PASSWORD))
    parser.add_argument('--scenario-file')
    parser.add_argument('--scenario-id', help='Only run one scenario from manifest')
    parser.add_argument('--interval', type=int, default=DEFAULT_INTERVAL)
    parser.add_argument('--log-scan-interval', type=int, default=DEFAULT_LOG_SCAN_INTERVAL)
    parser.add_argument('--report-dir', default=str(Path(__file__).resolve().parents[1] / 'reports'))
    parser.add_argument('--dashboard', action='store_true', help='Show live terminal dashboard with sparkline charts')
    parser.add_argument('--continuous', action='store_true', help='Run until Ctrl+C, ignoring scenario end time')
    parser.add_argument('--resolution', default='unknown')
    parser.add_argument('--target-fps', type=int, default=25)
    parser.add_argument('--task-count', type=int, default=1)
    parser.add_argument('--record-duration-s', type=int, default=600, help='Source recording duration per task in seconds')
    parser.add_argument('--layout-counts', default='1', help='Comma separated layout counts, e.g. 1,2,4,8')
    parser.add_argument('--continuous-scenario-id', default='continuous_monitor')
    parser.add_argument('--status-source', choices=['auto', 'record_control'], default=DEFAULT_STATUS_SOURCE)
    parser.add_argument('--record-control-host', default=DEFAULT_RECORD_CONTROL_HOST)
    parser.add_argument('--record-control-port', type=int, default=DEFAULT_RECORD_CONTROL_PORT)
    parser.add_argument('--record-control-username', default=DEFAULT_RECORD_CONTROL_USER)
    parser.add_argument('--record-control-password', default=os.environ.get(RECORD_CONTROL_PASSWORD_ENV, DEFAULT_RECORD_CONTROL_PASSWORD))
    parser.add_argument('--record-control-log-dir', default=REMOTE_RECORD_CONTROL_LOG)
    parser.add_argument('--ffprobe-bin', default='ffprobe')
    parser.add_argument('--store-download-url-api', default=DEFAULT_STORE_DOWNLOAD_URL_API)
    parser.add_argument('--store-app-key', default=os.environ.get(STORE_APP_KEY_ENV, DEFAULT_STORE_APP_KEY))
    parser.add_argument('--store-app-secret', default=os.environ.get(STORE_APP_SECRET_ENV, DEFAULT_STORE_APP_SECRET))
    return parser.parse_args()


def build_continuous_scenario(args) -> Scenario:
    return Scenario(
        scenario_id=args.continuous_scenario_id,
        start_time=None,
        end_time=None,
        resolution=args.resolution,
        target_fps=args.target_fps,
        task_count=args.task_count,
        layout_counts=[int(x) for x in args.layout_counts.split(',') if x.strip()],
        record_duration_s=args.record_duration_s,
    )


def main():
    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)
    args = parse_args()
    if not args.password:
        args.password = getpass.getpass('SSH password: ')
    if args.continuous:
        scenarios = [build_continuous_scenario(args)]
    else:
        if not args.scenario_file:
            raise SystemExit('--scenario-file is required unless --continuous is used')
        scenarios = load_manifest(Path(args.scenario_file))
        if args.scenario_id:
            scenarios = [scenario for scenario in scenarios if scenario.scenario_id == args.scenario_id]
            if not scenarios:
                raise SystemExit(f'Scenario not found: {args.scenario_id}')
        for scenario in scenarios:
            scenario.record_duration_s = args.record_duration_s

    collector = RemoteCollector(args.host, args.port, args.username, args.password)
    record_control_collector: Optional[RemoteCollector] = None
    status_source_used = 'disabled'

    collector.connect()
    try:
        if args.status_source in ('auto', 'record_control'):
            record_control_password = args.record_control_password
            if not record_control_password:
                record_control_password = getpass.getpass('Record-control SSH password: ')
            record_control_collector = RemoteCollector(
                args.record_control_host,
                args.record_control_port,
                args.record_control_username,
                record_control_password,
            )
            record_control_collector.connect()
            status_source_used = 'record_control'

        for scenario in scenarios:
            print(f'collecting scenario: {scenario.scenario_id}')
            collect_one_scenario(
                collector,
                scenario,
                args.interval,
                args.log_scan_interval,
                Path(args.report_dir),
                args.dashboard,
                args.continuous,
                record_control_collector=record_control_collector,
                status_source_used=status_source_used,
                record_control_log_dir=args.record_control_log_dir,
                ffprobe_bin=args.ffprobe_bin,
                store_download_url_api=args.store_download_url_api,
                store_app_key=args.store_app_key,
                store_app_secret=args.store_app_secret,
            )
    finally:
        collector.close()
        if record_control_collector:
            record_control_collector.close()

if __name__ == '__main__':
    main()
