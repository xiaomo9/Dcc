import os
import unittest
from unittest.mock import patch, MagicMock
import requests
from src import updates


def release(tag='v2.2.0'):
    name = 'DCC-macOS-arm64.dmg'
    return {'tag_name': tag, 'body': '<script>alert(1)</script>', 'assets': [
        {'name': name, 'state': 'uploaded', 'size': 100,
         'browser_download_url': f'{updates.REPOSITORY}/releases/download/{tag}/{name}'}]}


class UpdateTests(unittest.TestCase):
    def setUp(self):
        platform = patch.object(updates.sys, 'platform', 'linux')
        platform.start()
        self.addCleanup(platform.stop)

    def test_macos_timeout_uses_system_https_and_caches_success(self):
        completed = updates.subprocess.CompletedProcess([], 0, updates.REPOSITORY + '/releases/tag/v2.2.0', '')
        checker = updates.ReleaseChecker()
        with patch.object(updates.sys, 'platform', 'darwin'), \
             patch.object(updates.requests, 'get', side_effect=requests.Timeout) as get, \
             patch.object(updates.subprocess, 'run', return_value=completed) as run:
            result = checker.check()
            self.assertNotIn('error', result)
            self.assertEqual(result['latest'], 'v2.2.0')
            self.assertTrue(result['available'])
            self.assertFalse(result['download_url'])
            self.assertEqual(checker.check(), result)
            get.assert_called_once()
            run.assert_called_once()
            command = run.call_args.args[0]
            self.assertEqual(command[0], '/usr/bin/curl')
            self.assertIn('--max-time', command)
            self.assertNotIn('--insecure', command)
            self.assertFalse(run.call_args.kwargs.get('shell', False))

    def test_macos_native_failure_and_invalid_url_do_not_report_success(self):
        cases = [
            updates.subprocess.CompletedProcess([], 28, '', 'private details'),
            updates.subprocess.CompletedProcess([], 60, '', 'private details'),
            updates.subprocess.CompletedProcess([], 0, 'https://evil.example/tag/v9.0.0', ''),
            OSError('private details'),
            updates.subprocess.TimeoutExpired('curl', 10),
        ]
        for outcome in cases:
            with self.subTest(outcome=outcome), patch.object(updates.sys, 'platform', 'darwin'), \
                 patch.object(updates.requests, 'get', side_effect=requests.Timeout), \
                 patch.object(updates.subprocess, 'run') as run:
                if isinstance(outcome, Exception):
                    run.side_effect = outcome
                else:
                    run.return_value = outcome
                result = updates.ReleaseChecker().check()
                self.assertIn('error', result)
                self.assertFalse(result['available'])
                self.assertNotIn('private details', str(result))

    def test_windows_connection_failure_and_rate_limit_use_system_curl(self):
        limited = MagicMock()
        limited.status_code = 403
        limited.headers = {'X-RateLimit-Remaining': '0'}
        limited.__enter__.return_value = limited
        completed = updates.subprocess.CompletedProcess([], 0, updates.REPOSITORY + '/releases/tag/v2.2.0', '')
        for outcome in (requests.Timeout(), requests.ConnectionError(), limited):
            checker = updates.ReleaseChecker()
            with self.subTest(outcome=type(outcome).__name__), \
                 patch.object(updates.sys, 'platform', 'win32'), \
                 patch.dict(os.environ, {'SystemRoot': r'D:\Windows'}), \
                 patch.object(updates.subprocess, 'CREATE_NO_WINDOW', 0x08000000, create=True), \
                 patch.object(updates.requests, 'get', side_effect=[outcome]) as get, \
                 patch.object(updates.subprocess, 'run', return_value=completed) as run:
                result = checker.check()
                self.assertNotIn('error', result)
                self.assertEqual(result['latest'], 'v2.2.0')
                self.assertTrue(result['available'])
                self.assertFalse(result['download_url'])
                self.assertEqual(checker.check(), result)
                get.assert_called_once()
                run.assert_called_once()
                command = run.call_args.args[0]
                self.assertEqual(command[0], os.path.join(r'D:\Windows', 'System32', 'curl.exe'))
                self.assertEqual(command[command.index('--output') + 1], 'NUL')
                self.assertEqual(run.call_args.kwargs['creationflags'], 0x08000000)
                self.assertNotIn('--insecure', command)
                self.assertFalse(run.call_args.kwargs.get('shell', False))

    def test_windows_missing_curl_is_not_cached_and_can_retry(self):
        checker = updates.ReleaseChecker()
        completed = updates.subprocess.CompletedProcess([], 0, updates.REPOSITORY + '/releases/tag/v2.2.0', '')
        with patch.object(updates.sys, 'platform', 'win32'), \
             patch.object(updates.requests, 'get', side_effect=requests.Timeout), \
             patch.object(updates.subprocess, 'run', side_effect=[FileNotFoundError(), completed]) as run:
            self.assertIn('error', checker.check())
            result = checker.check()
            self.assertNotIn('error', result)
            self.assertEqual(result['latest'], 'v2.2.0')
            self.assertEqual(run.call_count, 2)

    def test_numeric_version_and_platform(self):
        self.assertGreater(updates.version('v2.1.13'), updates.version('2.1.9'))
        for system, arch, expected in [('darwin', 'arm64', 'DCC-macOS-arm64.dmg'),
                                       ('darwin', 'x86_64', 'DCC-macOS-x64.dmg'),
                                       ('win32', 'AMD64', 'DCC-Windows-x64-Setup.exe'),
                                       ('linux', 'x86_64', None)]:
            with patch.object(updates.sys, 'platform', system), patch.object(updates.platform, 'machine', return_value=arch):
                self.assertEqual(updates.asset_name(), expected)

    def test_new_same_old_prerelease_and_missing_asset(self):
        with patch.object(updates, 'DCC_VERSION', '2.1.13'), patch.object(updates, 'asset_name', return_value='DCC-macOS-arm64.dmg'):
            self.assertTrue(updates.describe(release())['download_url'])
            for tag in ('v2.1.13', 'v2.1.9'):
                self.assertFalse(updates.describe(release(tag))['available'])
            data = release()
            data['prerelease'] = True
            self.assertFalse(updates.describe(data)['available'])
            data = release()
            data['assets'][0]['browser_download_url'] = 'https://evil.example/install.exe'
            result = updates.describe(data)
            self.assertTrue(result['available'])
            self.assertFalse(result['download_url'])
            with self.assertRaises(ValueError):
                updates.describe(release('../../evil'))

    def test_success_cache_and_failure_retry(self):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = release()
        response.__enter__.return_value = response
        checker = updates.ReleaseChecker()
        with patch.object(updates.requests, 'get', side_effect=requests.Timeout):
            self.assertIn('error', checker.check())
        with patch.object(updates.requests, 'get', return_value=response) as get:
            self.assertNotIn('error', checker.check())
            checker.check()
            self.assertEqual(get.call_count, 1)
        response.status_code = 404
        with patch.object(updates.requests, 'get', return_value=response):
            self.assertEqual(updates.ReleaseChecker().check()['message'], '尚未发布正式版本')

    def test_failure_messages_identify_cause_without_exposing_details(self):
        cases = [
            (requests.Timeout('private details'), '连接 GitHub 超时，请检查网络或代理后重试'),
            (requests.exceptions.SSLError('private details'), 'GitHub 证书校验失败，请检查系统时间、证书或代理配置'),
            (requests.ConnectionError('private details'), '无法连接 GitHub，请检查网络或代理配置'),
            (ValueError('private details'), 'GitHub 版本信息格式无效，请稍后重试'),
        ]
        for error, message in cases:
            with self.subTest(error=type(error).__name__), \
                 patch.object(updates.requests, 'get', side_effect=error):
                result = updates.ReleaseChecker().check()
                self.assertEqual(result['error'], message)
                self.assertFalse(result['available'])
                self.assertNotIn('private details', str(result))

    def test_http_failure_identifies_status_and_allows_retry(self):
        for status in (403, 429, 500):
            response = MagicMock()
            response.status_code = status
            response.__enter__.return_value = response
            response.raise_for_status.side_effect = requests.HTTPError(response=response)
            checker = updates.ReleaseChecker()
            with self.subTest(status=status), patch.object(updates.requests, 'get', return_value=response):
                self.assertEqual(checker.check()['error'], f'GitHub 更新接口返回 HTTP {status}，请稍后重试')
            response.raise_for_status.side_effect = None
            response.status_code = 200
            response.json.return_value = release()
            with patch.object(updates.requests, 'get', return_value=response) as get:
                self.assertNotIn('error', checker.check())
                get.assert_called_once()

    def test_rate_limit_fallback_timeout_is_reported(self):
        response = MagicMock()
        response.status_code = 403
        response.headers = {'X-RateLimit-Remaining': '0'}
        response.__enter__.return_value = response
        response.raise_for_status.side_effect = requests.HTTPError(response=response)
        with patch.object(updates.requests, 'get', side_effect=[response, requests.Timeout]):
            self.assertEqual(updates.ReleaseChecker().check()['error'],
                             '连接 GitHub 超时，请检查网络或代理后重试')

    def test_rate_limit_falls_back_to_official_latest_page_and_caches(self):
        limited = MagicMock()
        limited.status_code = 403
        limited.headers = {'X-RateLimit-Remaining': '0'}
        limited.__enter__.return_value = limited
        for tag, available in [('v2.2.0', True), ('v2.1.17', False), ('v2.1.9', False)]:
            page = MagicMock()
            page.url = updates.REPOSITORY + '/releases/tag/' + tag
            page.__enter__.return_value = page
            checker = updates.ReleaseChecker()
            with self.subTest(tag=tag), patch.object(updates, 'DCC_VERSION', '2.1.17'), \
                 patch.object(updates.requests, 'get', side_effect=[limited, page]) as get:
                result = checker.check()
                self.assertNotIn('error', result)
                self.assertEqual(result['latest'], tag)
                self.assertEqual(result['available'], available)
                self.assertEqual(result['download_url'], '')
                self.assertEqual(result['release_url'], page.url)
                self.assertNotIn('安装包尚未提供', result['message'])
                self.assertEqual(checker.check(), result)
                self.assertEqual(get.call_count, 2)
                self.assertEqual(get.call_args.args[0], updates.REPOSITORY + '/releases/latest')

    def test_fallback_rejects_unexpected_redirects(self):
        limited = MagicMock()
        limited.status_code = 403
        limited.headers = {'X-RateLimit-Remaining': '0'}
        limited.__enter__.return_value = limited
        for url in ['https://evil.example/releases/tag/v9.0.0',
                    updates.REPOSITORY + '/releases',
                    updates.REPOSITORY + '/releases/tag/v2.2.0-beta',
                    updates.REPOSITORY + '/releases/tag/v2.2.0?other=1']:
            page = MagicMock()
            page.url = url
            page.__enter__.return_value = page
            with self.subTest(url=url), \
                 patch.object(updates.requests, 'get', side_effect=[limited, page]):
                result = updates.ReleaseChecker().check()
                self.assertIn('error', result)
                self.assertFalse(result['available'])
