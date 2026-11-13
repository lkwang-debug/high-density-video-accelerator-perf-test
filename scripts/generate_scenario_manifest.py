#!/usr/bin/env python3
import argparse
import csv
import json
from pathlib import Path


def parse_layout_counts(value: str):
    return [int(x.strip()) for x in value.split(',') if x.strip()]


def build_scenario(row):
    target_fps = int(row.get('target_fps', 25))
    task_count = int(row['task_count'])
    layout_counts = parse_layout_counts(row['layout_counts'])
    return {
        'scenario_id': row['scenario_id'],
        'start_time': row['start_time'],
        'end_time': row['end_time'],
        'resolution': row.get('resolution', 'unknown'),
        'target_fps': target_fps,
        'task_count': task_count,
        'layout_counts': layout_counts,
        'expected_total_decode_fps': sum(layout_counts) * target_fps,
        'expected_encoder_input_fps': task_count * target_fps,
        'expected_encoder_output_fps': task_count * target_fps,
    }


def main():
    parser = argparse.ArgumentParser(description='Generate scenario_manifest.json from CSV task windows.')
    parser.add_argument('--input-csv', required=True)
    parser.add_argument('--output-json', required=True)
    args = parser.parse_args()

    rows = []
    with Path(args.input_csv).open('r', encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not row.get('scenario_id'):
                continue
            rows.append(build_scenario(row))

    output = {'scenarios': rows}
    Path(args.output_json).write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')
    print(args.output_json)

if __name__ == '__main__':
    main()
