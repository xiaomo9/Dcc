"""Run from the repository root on the target operating system."""
from pathlib import Path
import subprocess
import sys
from PIL import Image

root = Path(__file__).resolve().parents[1]
(root / 'build').mkdir(exist_ok=True)
with Image.open(root / 'docs/assets/dcc-icon-pink.png') as image:
    image.save(root / 'build' / ('dcc.icns' if sys.platform == 'darwin' else 'dcc.ico'))
subprocess.run([sys.executable, '-m', 'PyInstaller', '--clean', '--noconfirm',
                str(root / 'packaging/dcc.spec')], cwd=root, check=True)
