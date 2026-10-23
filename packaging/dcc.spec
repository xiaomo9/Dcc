from pathlib import Path
import sys

root = Path(SPECPATH).parent
mac = sys.platform == 'darwin'
icon = str(root / 'build' / ('dcc.icns' if mac else 'dcc.ico'))
assets = [(str(root / 'src' / name), 'src') for name in
          ('ui.html', 'guide.html', 'guide_intro.html', 'VERSION')]
assets += [(str(root / name), str(Path(name).parent)) for name in
           ('getting-started.md', 'dcc.md', 'docs/design.md', 'docs/known-issues.md',
            'docs/assets/dcc-icon-pink.png')]
a = Analysis([str(root / 'packaging/entry.py')], pathex=[str(root), str(root / 'src')],
             binaries=[], datas=assets, hiddenimports=['src.proxy_server'],
             hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='DCC',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, icon=icon)
cli = EXE(pyz, a.scripts, [], exclude_binaries=True, name='dcc-cli',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=True, icon=icon)
collection = COLLECT(exe, cli, a.binaries, a.datas, strip=False, upx=False, name='DCC')
if mac:
    app = BUNDLE(collection, name='DCC.app', icon=icon,
                 bundle_identifier='local.dcc.desktop',
                 version=(root / 'src/VERSION').read_text().strip())
