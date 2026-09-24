import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import proxy_server as proxy


class OpenAIStreamTests(unittest.TestCase):
    def convert(self, pieces, *, mode='hide', tool=True):
        chunks = [{'choices': [{'delta': {'content': piece}}]} for piece in pieces]
        if tool:
            chunks.append({'choices': [{'delta': {'tool_calls': [{
                'index': 0, 'id': 'call_test', 'type': 'function',
                'function': {'name': 'Bash', 'arguments': '{"command":"git status"}'},
            }]}}]})
        chunks.append({'choices': [{'delta': {}, 'finish_reason': 'tool_calls' if tool else 'stop'}]})
        response = MagicMock()
        response.__enter__.return_value = response
        response.status_code = 200
        response.iter_lines.return_value = iter(
            ['data: ' + json.dumps(chunk) for chunk in chunks] + ['data: [DONE]']
        )
        handler = SimpleNamespace(wfile=io.BytesIO())
        model = {'upstream_id': 'test', 'base_url': 'https://invalid.invalid', 'api_key': 'test'}
        config = SimpleNamespace(soft_timeout=10, max_candidates=1)
        with patch.object(proxy, 'CONFIG', config, create=True), \
                patch.object(proxy, '_reasoning_mode', return_value=mode), \
                patch.object(proxy, '_log'), patch.object(proxy, '_tl'), \
                patch.object(proxy.requests, 'post', return_value=response):
            proxy.stream_openai_to_anthropic(handler, 'test', model, [], {}, {})
        return [json.loads(line[6:]) for line in handler.wfile.getvalue().decode().splitlines()
                if line.startswith('data: ')]

    def assert_valid_blocks(self, events):
        open_blocks = set()
        started_blocks = set()
        for event in events:
            kind, index = event['type'], event.get('index')
            if kind == 'content_block_start':
                self.assertNotIn(index, started_blocks, 'content block started twice')
                started_blocks.add(index)
                open_blocks.add(index)
            elif kind in ('content_block_delta', 'content_block_stop'):
                self.assertIn(index, open_blocks, f'{kind} references a closed or unstarted block')
                if kind == 'content_block_stop':
                    open_blocks.remove(index)
        self.assertFalse(open_blocks)
        self.assertEqual(events[-1]['type'], 'message_stop')

    def text(self, events):
        return ''.join(event['delta']['text'] for event in events
                       if event['type'] == 'content_block_delta'
                       and event['delta']['type'] == 'text_delta')

    def test_text_is_complete_before_tool_starts(self):
        for mode in ('hide', 'thinking', 'text'):
            for text in ('好的，我先获取提交，然后查看内容。', '好的', '123456'):
                with self.subTest(mode=mode, text=text):
                    events = self.convert([text[:2], text[2:]], mode=mode)
                    self.assert_valid_blocks(events)
                    tool_start = next(i for i, event in enumerate(events)
                                      if event['type'] == 'content_block_start'
                                      and event['content_block']['type'] == 'tool_use')
                    self.assertEqual(self.text(events[:tool_start]), text)
                    self.assertEqual(self.text(events[tool_start:]), '')
                    arguments = ''.join(event['delta']['partial_json'] for event in events
                                         if event['type'] == 'content_block_delta'
                                         and event['delta']['type'] == 'input_json_delta')
                    self.assertEqual(json.loads(arguments), {'command': 'git status'})
                    self.assertEqual(events[-2]['delta']['stop_reason'], 'tool_use')

    def test_text_only_flushes_trailing_characters_once(self):
        for text in ('普通回复的最后六个字符也应保留。', '好的'):
            with self.subTest(text=text):
                events = self.convert([text], tool=False)
                self.assert_valid_blocks(events)
                self.assertEqual(self.text(events), text)
                self.assertEqual(events[-2]['delta']['stop_reason'], 'end_turn')

    def test_tool_only_has_no_empty_text_block(self):
        events = self.convert([])
        self.assert_valid_blocks(events)
        blocks = [event['content_block']['type'] for event in events
                  if event['type'] == 'content_block_start']
        self.assertEqual(blocks, ['tool_use'])

    def test_split_thinking_tags_do_not_leak_into_text(self):
        for mode in ('hide', 'thinking'):
            with self.subTest(mode=mode):
                events = self.convert(['<thi', 'nk>internal reasoning</thi', 'nk>好了'], mode=mode)
                self.assert_valid_blocks(events)
                self.assertEqual(self.text(events), '好了')


if __name__ == '__main__':
    unittest.main()
