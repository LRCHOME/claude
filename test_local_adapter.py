import copy
import unittest
from local_adapter import normalize_system


class NormalizeSystemTests(unittest.TestCase):
    def test_claude_environment_after_user(self):
        data = {'system': [{'type': 'text', 'text': 'instructions'}],
                'messages': [{'role': 'user', 'content': 'request'},
                             {'role': 'system', 'content': [{'type': 'text', 'text': 'environment'}]}],
                'stream': True, 'tools': [{'name': 'Read'}]}
        original = copy.deepcopy(data)
        result = normalize_system(data)
        self.assertEqual(result['system'], 'instructions\n\nenvironment')
        self.assertEqual(result['messages'], [data['messages'][0]])
        self.assertEqual(result['tools'], data['tools'])
        self.assertTrue(result['stream'])
        self.assertEqual(data, original)

    def test_tool_result_and_thinking_are_preserved(self):
        data = {'messages': [
            {'role': 'assistant', 'content': [{'type': 'thinking', 'thinking': 'reason'},
                                             {'type': 'tool_use', 'id': 't1', 'name': 'Read', 'input': {'file_path': 'a'}}]},
            {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 't1', 'content': 'answer'}]}]}
        self.assertEqual(normalize_system(data), data)

    def test_string_system_and_multiple_inline_systems(self):
        data = {'system': 'a', 'messages': [{'role': 'system', 'content': 'b'},
                                          {'role': 'system', 'content': 'c'}]}
        self.assertEqual(normalize_system(data), {'system': 'a\n\nb\n\nc', 'messages': []})

    def test_unsupported_system_blocks_are_not_silently_lost(self):
        with self.assertRaises(ValueError):
            normalize_system({'system': [{'type': 'image', 'source': {}}]})


if __name__ == '__main__':
    unittest.main()
