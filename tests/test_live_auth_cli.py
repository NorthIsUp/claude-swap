"""Opt-in contract test against an installed Claude CLI, with fake local auth.

CLAUDE_SWAP_TEST_CLAUDE=/absolute/path/to/claude python -m pytest \
    tests/test_live_auth_cli.py -n 0 -q
No paid API requests are made. The server records only credential labels.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher

CLI = os.environ.get('CLAUDE_SWAP_TEST_CLAUDE')


@pytest.mark.skipif(not CLI, reason='set CLAUDE_SWAP_TEST_CLAUDE for the real CLI contract test')
def test_running_claude_switches_oauth_and_keys_without_respawning(temp_home, monkeypatch):
    oauth = 'sk-ant-oat01-local-fake-oauth'
    key1 = 'sk-ant-api03-local-fake-key-' + '1' * 30
    key2 = 'sk-ant-api03-local-fake-key-' + '2' * 30
    observed = queue.Queue()
    output = queue.Queue()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            if '/messages' not in self.path:
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{}')
                return
            auth = self.headers.get('Authorization')
            key = self.headers.get('x-api-key')
            observed.put('oauth' if auth == 'Bearer ' + oauth else
                         'key1' if key == key1 else 'key2' if key == key2 else 'unexpected')
            msg = {'id': 'msg_fake', 'type': 'message', 'role': 'assistant',
                   'model': body['model'], 'content': [], 'stop_reason': None,
                   'stop_sequence': None, 'usage': {'input_tokens': 10, 'output_tokens': 0}}
            frames = [
                ('message_start', {'type': 'message_start', 'message': msg}),
                ('content_block_start', {'type': 'content_block_start', 'index': 0,
                                         'content_block': {'type': 'text', 'text': ''}}),
                ('content_block_delta', {'type': 'content_block_delta', 'index': 0,
                                         'delta': {'type': 'text_delta', 'text': 'OK'}}),
                ('content_block_stop', {'type': 'content_block_stop', 'index': 0}),
                ('message_delta', {'type': 'message_delta',
                                   'delta': {'stop_reason': 'end_turn', 'stop_sequence': None},
                                   'usage': {'output_tokens': 1}}),
                ('message_stop', {'type': 'message_stop'}),
            ]
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            for name, data in frames:
                self.wfile.write(('event: ' + name + '\ndata: ' + json.dumps(data) + '\n\n').encode())

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    profile = temp_home / '.claude'
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(profile))
    monkeypatch.setenv('CLAUDE_SECURESTORAGE_CONFIG_DIR', str(profile))
    monkeypatch.setattr('claude_swap.oauth.fetch_oauth_profile', lambda *a, **kw: None)
    switcher = ClaudeAccountSwitcher()
    switcher.platform = Platform.LINUX  # no real Keychain writes, even on macOS
    for slot, credential in ((1, oauth), (2, key1), (3, key2)):
        switcher.add_account_from_token(credential, slot=slot)
    # The setup token is also a usable fake OAuth profile for the native CLI.
    raw = json.dumps({'claudeAiOauth': {
        'accessToken': oauth, 'scopes': ['user:inference', 'user:profile'],
        'expiresAt': int((time.time() + 3600) * 1000), 'subscriptionType': 'max',
    }})
    switcher._write_account_credentials('1', 'setup-token-1@token.local', raw)
    settings = profile / 'settings.json'
    url = f'http://127.0.0.1:{server.server_port}'
    settings.write_text(json.dumps({'env': {'ANTHROPIC_BASE_URL': url}}))
    switcher._perform_switch('1', emit_output=False)
    config = profile / '.claude.json'
    data = json.loads(config.read_text())
    data.update(hasCompletedOnboarding=True, theme='dark')
    config.write_text(json.dumps(data))
    env = dict(os.environ, ANTHROPIC_BASE_URL=url,
               CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1', DISABLE_TELEMETRY='1',
               DISABLE_ERROR_REPORTING='1')
    proc = subprocess.Popen([
        CLI, '-p', '--input-format', 'stream-json', '--output-format', 'stream-json',
        '--verbose', '--no-session-persistence', '--tools', '', '--strict-mcp-config',
        '--mcp-config', '{"mcpServers":{}}', '--setting-sources', 'user',
        '--model', 'claude-sonnet-4-5',
    ], cwd=temp_home, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True)

    def read(stream):
        for line in stream:
            output.put(line)

    threading.Thread(target=read, args=(proc.stdout,), daemon=True).start()
    threading.Thread(target=read, args=(proc.stderr,), daemon=True).start()
    pid = proc.pid
    try:
        transitions = (('1', 'oauth'), ('2', 'key1'), ('3', 'key2'),
                       ('1', 'oauth'), ('2', 'key1'))
        for index, (slot, expected) in enumerate(transitions):
            # First repair an already-active slot, then use ordinary switches.
            switcher._perform_switch(slot, emit_output=False, force_activate=index == 0)
            time.sleep(2)  # Claude's settings watcher debounces changes.
            proc.stdin.write(json.dumps({'type': 'user', 'message': {
                'role': 'user', 'content': 'Say OK'}, 'parent_tool_use_id': None,
                'session_id': ''}) + '\n')
            proc.stdin.flush()
            deadline = time.monotonic() + 30
            result = None
            while time.monotonic() < deadline:
                try:
                    line = output.get(timeout=1)
                except queue.Empty:
                    assert proc.poll() is None, 'Claude exited during the live switch'
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if message.get('type') == 'result':
                    result = message
                    break
            assert result is not None, 'Claude did not finish the fake request'
            assert not result.get('is_error'), result
            assert observed.get(timeout=2) == expected
            assert observed.empty(), 'unexpected additional model request'
            assert proc.pid == pid and proc.poll() is None
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        server.shutdown()
        server.server_close()
