import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import run_hard_encode_excel_workflow as workflow
import generate_scenario_manifest as generator


class CommandLineTests(unittest.TestCase):
    def test_help_does_not_require_local_workbook(self):
        with patch.object(sys,'argv',['workflow','--help']), patch.object(workflow,'find_default_workbook',side_effect=AssertionError('must not discover workbook')), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as result:workflow.parse_args()
        self.assertEqual(result.exception.code,0)

    def test_explicit_workbook_does_not_require_repository_results(self):
        with patch.object(sys,'argv',['workflow','--workbook','D:/sample.xlsx','--dry-run']), patch.object(workflow,'find_default_workbook',side_effect=AssertionError('must use explicit path')):
            args=workflow.parse_args()
        self.assertEqual(args.workbook,'D:/sample.xlsx');self.assertTrue(args.dry_run)

    def test_configured_workbook_does_not_evaluate_default(self):
        with tempfile.TemporaryDirectory() as directory:
            config=Path(directory)/'environment.json'
            config.write_text(json.dumps({'paths':{'workbook':'D:/configured.xlsx'}}),encoding='utf-8')
            with patch.object(sys,'argv',['workflow','--env-config',str(config)]), patch.object(workflow,'find_default_workbook',side_effect=AssertionError('must use configured path')):
                self.assertEqual(workflow.parse_args().workbook,'D:/configured.xlsx')

    def test_default_discovery_remains_available(self):
        with patch.object(sys,'argv',['workflow']), patch.object(workflow,'find_default_workbook',return_value=Path('existing.xlsx')) as discover:
            self.assertEqual(workflow.parse_args().workbook,'existing.xlsx')
        discover.assert_called_once_with()

    def test_missing_workbook_explains_required_argument(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(workflow,'PROJECT_ROOT',Path(directory)), patch.object(sys,'argv',['workflow']):
            with self.assertRaisesRegex(SystemExit,'pass --workbook explicitly'):workflow.parse_args()

    def test_offline_scenario_calculations(self):
        scenario=generator.build_scenario({'scenario_id':'two-tasks','start_time':'','end_time':'',
            'resolution':'1080p','target_fps':'25','task_count':'2','layout_counts':'25,25'})
        self.assertEqual(scenario['expected_total_decode_fps'],1250)
        self.assertEqual(scenario['expected_encoder_input_fps'],50)
        self.assertEqual(scenario['expected_encoder_output_fps'],50)


if __name__=='__main__':unittest.main()
