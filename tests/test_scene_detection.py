import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


class SceneDetectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Execute the pure classification methods without importing GUI/OpenCV.
        tree = ast.parse(Path('autofdx/vision_service.py').read_text(encoding='utf-8'))
        functions = []
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in ('detect_home_page', 'format_home_page_scene_label'):
                node.decorator_list = []
                functions.append(node)
        ns = {'HOME_PAGE_TEMPLATE_SHORT': {'start':'开始', 'recover_stamina_button':'恢复体力'}}
        exec(compile(ast.Module(body=functions, type_ignores=[]), 'scene_methods', 'exec'), ns)
        cls.detect = staticmethod(ns['detect_home_page'])
        cls.label = staticmethod(ns['format_home_page_scene_label'])

    def test_experiment_detected_without_home_pool(self):
        fake = SimpleNamespace(probe_flow_templates=Mock(return_value=['start']), is_like_pool_present=Mock(return_value=False))
        home, matched, blockers = self.detect(fake)
        self.assertFalse(home)
        self.assertEqual(matched, ['start'])
        self.assertTrue(self.label(home, matched, blockers).startswith('实验内'))
        fake.is_like_pool_present.assert_not_called()

    def test_home_requires_positive_pool_and_no_flow_markers(self):
        fake = SimpleNamespace(probe_flow_templates=lambda:[], is_like_pool_present=lambda:True)
        self.assertEqual(self.detect(fake), (True, [], []))

    def test_unknown_is_not_reported_as_experiment_or_home(self):
        fake = SimpleNamespace(probe_flow_templates=lambda:[], is_like_pool_present=lambda:False)
        self.assertTrue(self.label(*self.detect(fake)).startswith('页面未知'))

    def test_panel_marker_is_not_enough_to_claim_deployment(self):
        self.assertTrue(self.label(False, ['body_part_switch']).startswith('非主页'))

    def test_flow_marker_wins_even_when_pool_is_visible(self):
        fake = SimpleNamespace(probe_flow_templates=lambda:['recover_stamina_button'], is_like_pool_present=lambda:True)
        result=self.detect(fake)
        self.assertFalse(result[0])
        self.assertTrue(self.label(*result).startswith('实验内'))

if __name__ == '__main__':
    unittest.main()
