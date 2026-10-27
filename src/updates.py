"""Read-only release discovery. Installers are downloaded by the user's browser."""
import os
import platform
import re
import subprocess
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


def describe_latest_page():
    url = REPOSITORY + '/releases/latest'
    if sys.platform in ('darwin', 'win32'):
        windows = sys.platform == 'win32'
        executable = (os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32', 'curl.exe')
                      if windows else '/usr/bin/curl')
        try:
            response = subprocess.run(
                [executable, '-q', '--head', '--location', '--fail', '--silent', '--show-error',
                 '--proto', '=https', '--proto-redir', '=https', '--max-redirs', '3',
                 '--connect-timeout', '3', '--max-time', '8', '--output', 'NUL' if windows else '/dev/null',
                 '--write-out', '%{url_effective}', url],
                capture_output=True, text=True, timeout=10,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) if windows else 0)
        except subprocess.TimeoutExpired as error:
            raise requests.Timeout() from error
        except OSError as error:
            raise requests.ConnectionError() from error
        if response.returncode == 28:
            raise requests.Timeout()
        if response.returncode == 60:
            raise requests.exceptions.SSLError()
        if response.returncode:
            raise requests.ConnectionError()
        url = response.stdout.strip()
    else:
        with requests.get(url, headers={'User-Agent': 'DCC-update-check'},
                          timeout=(3, 8), stream=True) as response:
            response.raise_for_status()
            url = response.url
    match = re.fullmatch(re.escape(REPOSITORY) + r'/releases/tag/(v?\d+\.\d+\.\d+)', url)
    if not match:
        raise ValueError('版本页面地址无效')
    tag = match.group(1)
    result = describe({'tag_name': tag})
    result['notes'] = '请打开官方发布页面查看更新说明及安装包。'
    if result['available']:
        result['message'] = '发现新版本 ' + tag + '，请前往发布页面下载'
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
            used_page = False
            try:
                with requests.get(API, headers={'Accept': 'application/vnd.github+json',
                                               'User-Agent': 'DCC-update-check'}, timeout=(3, 8)) as response:
                    if (response.status_code == 403
                            and response.headers.get('X-RateLimit-Remaining') == '0'):
                        used_page = True
                        result = describe_latest_page()
                    elif response.status_code == 404:
                        result = {'source_mode': not getattr(sys, 'frozen', False), 'current': DCC_VERSION, 'available': False,
                                  'message': '尚未发布正式版本', 'download_url': '',
                                  'release_url': REPOSITORY + '/releases', 'notes': ''}
                    else:
                        response.raise_for_status()
                        result = describe(response.json())
            except (requests.RequestException, ValueError, TypeError, KeyError) as error:
                if (sys.platform in ('darwin', 'win32') and not used_page
                        and isinstance(error, (requests.Timeout, requests.ConnectionError))):
                    try:
                        result = describe_latest_page()
                    except (requests.RequestException, ValueError, TypeError, KeyError) as fallback_error:
                        error = fallback_error
                    else:
                        self._cached = result
                        self._expires = time.monotonic() + 300
                        return dict(result)
                message = '检查更新失败，请稍后重试'
                if isinstance(error, requests.Timeout):
                    message = '连接 GitHub 超时，请检查网络或代理后重试'
                elif isinstance(error, requests.exceptions.SSLError):
                    message = 'GitHub 证书校验失败，请检查系统时间、证书或代理配置'
                elif isinstance(error, requests.ConnectionError):
                    message = '无法连接 GitHub，请检查网络或代理配置'
                elif isinstance(error, requests.HTTPError) and error.response is not None:
                    message = f'GitHub 更新接口返回 HTTP {error.response.status_code}，请稍后重试'
                    if (error.response.status_code == 403
                            and error.response.headers.get('X-RateLimit-Remaining') == '0'):
                        message = 'GitHub 更新接口请求额度已用尽，请稍后重试'
                elif isinstance(error, (ValueError, TypeError, KeyError)):
                    message = 'GitHub 版本信息格式无效，请稍后重试'
                return {'source_mode': not getattr(sys, 'frozen', False), 'current': DCC_VERSION, 'available': False, 'error': message}
            self._cached = result
            self._expires = time.monotonic() + 300
            return dict(result)
