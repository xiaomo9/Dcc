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
