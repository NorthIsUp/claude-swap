"""The settings bridge must change auth without losing preferences or sessions."""
import json
from unittest.mock import patch

import pytest

from claude_swap.exceptions import SwitchError
from claude_swap.live_auth import LIVE_AUTH_MARKER, publish_live_auth
from claude_swap.models import Platform
from claude_swap.session import SessionManager
from claude_swap.switcher import ClaudeAccountSwitcher

KEY = 'sk-ant-api03-fake-test-key-' + 'x' * 30
OAUTH = json.dumps({'claudeAiOauth': {'accessToken': 'sk-ant-oat01-fake'}})


def test_live_auth_retains_preferences_and_explicitly_clears_key(temp_home):
    settings = temp_home / '.claude/settings.json'
    settings.write_text(json.dumps({'model': 'sonnet', 'env': {'KEEP': 'yes'}}))
    publish_live_auth(KEY, temp_home / 'backup')
    data = json.loads(settings.read_text())
    assert data['env']['ANTHROPIC_API_KEY'] == KEY
    assert data['env'][LIVE_AUTH_MARKER] == '1'
    assert data['model'] == 'sonnet'
    assert data['env']['KEEP'] == 'yes'
    assert settings.stat().st_mode & 0o777 == 0o600
    publish_live_auth(OAUTH, temp_home / 'backup')
    assert json.loads(settings.read_text())['env']['ANTHROPIC_API_KEY'] == ''
    assert KEY not in settings.read_text()


def test_dotfiles_symlink_survives_publish(temp_home):
    settings = temp_home / '.claude/settings.json'
    real = temp_home / 'dotfiles-settings.json'
    real.write_text('{"model":"sonnet"}')
    settings.symlink_to(real)
    publish_live_auth(KEY, temp_home / 'backup')
    assert settings.is_symlink()
    assert json.loads(real.read_text())['env']['ANTHROPIC_API_KEY'] == KEY


@pytest.mark.parametrize('broken', ['{"env":', '[]', '{"env":[]}'])
def test_bad_settings_are_not_overwritten(temp_home, broken):
    settings = temp_home / '.claude/settings.json'
    settings.write_text(broken)
    with pytest.raises(SwitchError, match='left in place'):
        publish_live_auth(KEY, temp_home / 'backup')
    assert settings.read_text() == broken


def test_atomic_write_failure_does_not_publish_key(temp_home):
    settings = temp_home / '.claude/settings.json'
    settings.write_text('{"model":"sonnet"}')
    with patch('claude_swap.live_auth.replace_with_retry', side_effect=OSError('disk full')):
        with pytest.raises(OSError):
            publish_live_auth(KEY, temp_home / 'backup')
    assert settings.read_text() == '{"model":"sonnet"}'


def test_existing_isolated_session_keeps_its_auth(temp_home):
    backup = temp_home / 'backup'
    settings = temp_home / '.claude/settings.json'
    settings.write_text('{"model":"sonnet","env":{"KEEP":"yes"}}')
    session = backup / 'sessions/1-other'
    session.mkdir(parents=True)
    (session / 'settings.json').symlink_to(settings)
    (session / '.cswap-shared.json').write_text('{"items":["settings.json"]}')
    publish_live_auth(KEY, backup)
    isolated = session / 'settings.json'
    assert not isolated.is_symlink()
    assert json.loads(isolated.read_text()) == {'model': 'sonnet', 'env': {'KEEP': 'yes'}}
    assert json.loads(settings.read_text())['env']['ANTHROPIC_API_KEY'] == KEY


def test_new_isolated_session_does_not_inherit_default_key(temp_home):
    s = ClaudeAccountSwitcher()
    s.platform = Platform.LINUX
    s.add_account_from_token(KEY, slot=1)
    publish_live_auth(KEY, s.backup_dir)
    mgr = SessionManager(s)
    profile, _, _ = mgr.setup_session('1', share=True)
    data = json.loads((profile / 'settings.json').read_text())
    assert 'ANTHROPIC_API_KEY' not in data['env']
    assert LIVE_AUTH_MARKER not in data['env']
    assert KEY not in (profile / 'settings.json').read_text()
    assert 'settings.json' in json.loads((profile / '.cswap-shared.json').read_text())['items']


@pytest.mark.parametrize('force', [False, True])
def test_failed_live_publish_rolls_back_account_switch(temp_home, force):
    s = ClaudeAccountSwitcher()
    s.platform = Platform.LINUX
    s.add_account_from_token('sk-ant-oat01-fake', slot=1)
    s.add_account_from_token(KEY, slot=2)
    s._perform_switch('1', emit_output=False)
    before = s._read_credentials()
    with patch('claude_swap.live_auth.publish_live_auth', side_effect=OSError('disk full')):
        with pytest.raises((SwitchError, OSError)):
            s._perform_switch('2', emit_output=False, force_activate=force)
    assert s._get_sequence_data()['activeAccountNumber'] == 1
    assert s._read_credentials() == before
    assert json.loads((temp_home / '.claude/settings.json').read_text())['env']['ANTHROPIC_API_KEY'] == ''


def test_selecting_already_active_key_repairs_running_process_override(temp_home):
    s = ClaudeAccountSwitcher()
    s.platform = Platform.LINUX
    s.add_account_from_token(KEY, slot=1)
    s._perform_switch('1', emit_output=False)
    settings = temp_home / '.claude/settings.json'
    settings.write_text('{"env":{"ANTHROPIC_API_KEY":""}}')
    before = s._read_credentials()
    result = s.switch_to('1', json_output=True)
    assert result['reason'] == 'already-active'
    assert json.loads(settings.read_text())['env']['ANTHROPIC_API_KEY'] == KEY
    assert s._read_credentials() == before
