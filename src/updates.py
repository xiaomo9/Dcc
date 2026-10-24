"""Read-only release discovery. Installers are downloaded by the user's browser."""
import platform
import re
import sys
import threading
import time

import requests

from .version import DCC_VERSION

REPOSITORY = 'https://github.com/xiaomo9/Dcc'
API = 'https://api.github.com/repos/xiaomo9/Dcc/releases/latest'


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r'v?\d+\.\d+\.\d+', value):
        raise ValueError('版本号格式无效')
    return tuple(int(part) for part in value.removeprefix('v').split('.'))


def asset_name():
    arch = platform.machine().lower()
    if sys.platform == 'darwin' and arch in ('arm64', 'aarch64', 'x86_64', 'amd64'):
        return 'DCC-macOS-' + ('arm64' if arch in ('arm64', 'aarch64') else 'x64') + '.dmg'
    if sys.platform == 'win32' and arch in ('amd64', 'x86_64'):
        return 'DCC-Windows-x64-Setup.exe'
    return None


def describe(release):
    result = {'source_mode': not getattr(sys, 'frozen', False), 'current': DCC_VERSION, 'available': False, 'download_url': '',
              'release_url': REPOSITORY + '/releases', 'notes': ''}
    if not isinstance(release, dict):
        raise ValueError('版本信息格式无效')
    if release.get('draft') or release.get('prerelease'):
        return {**result, 'message': '暂无新的正式版本'}
    tag = release.get('tag_name')
    newer = version(tag) > version(DCC_VERSION)
    result.update(latest=tag, available=newer,
                  release_url=REPOSITORY + '/releases/tag/' + tag,
                  notes=str(release.get('body') or '此版本未填写更新说明。')[:20000])
    if not newer:
        return {**result, 'message': '当前版本已是最新'}
    name = asset_name()
    expected = f'{REPOSITORY}/releases/download/{tag}/{name}'
    for asset in release.get('assets', []):
        if (isinstance(asset, dict) and asset.get('name') == name
                and asset.get('state') == 'uploaded' and asset.get('size', 0) > 0
                and asset.get('browser_download_url') == expected):
            result['download_url'] = expected
            break
    result['message'] = '发现新版本 ' + tag
    if not result['download_url']:
        result['message'] += '，当前系统安装包尚未提供'
    return result


class ReleaseChecker:
    def __init__(self):
        self._lock = threading.Lock()
        self._cached = None
        self._expires = 0

    def check(self):
        with self._lock:
            if self._cached is not None and time.monotonic() < self._expires:
                return dict(self._cached)
            try:
                with requests.get(API, headers={'Accept': 'application/vnd.github+json',
                                               'User-Agent': 'DCC-update-check'}, timeout=(3, 8)) as response:
                    if response.status_code == 404:
                        result = {'source_mode': not getattr(sys, 'frozen', False), 'current': DCC_VERSION, 'available': False,
                                  'message': '尚未发布正式版本', 'download_url': '',
                                  'release_url': REPOSITORY + '/releases', 'notes': ''}
                    else:
                        response.raise_for_status()
                        result = describe(response.json())
            except (requests.RequestException, ValueError, TypeError, KeyError):
                return {'source_mode': not getattr(sys, 'frozen', False), 'current': DCC_VERSION, 'available': False, 'error': '检查更新失败，请稍后重试'}
            self._cached = result
            self._expires = time.monotonic() + 300
            return dict(result)
