"""Build public release assets from an explicit allowlist, never local app data."""
import ast
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / '.build'
DIST = ROOT / 'dist'
NAME = 'E621TicketBot'


def main():
    if sys.platform != 'win32':
        raise SystemExit('Build the Windows release on Windows.')
    BUILD.mkdir(exist_ok=True)
    DIST.mkdir(exist_ok=True)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1',
               PYINSTALLER_CONFIG_DIR=str(BUILD / 'pyinstaller-cache'),
               TEMP=str(BUILD), TMP=str(BUILD))
    source = ROOT / 'ticket_bot.pyw'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    version = next(ast.literal_eval(n.value) for n in tree.body
                   if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'VERSION' for t in n.targets))
    subprocess.run([sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_*.py'],
                   cwd=ROOT, env=env, check=True)
    subprocess.run([sys.executable, '-B', '-m', 'PyInstaller', '--noconfirm', '--clean',
                    '--onedir', '--windowed', '--name', NAME, '--noupx',
                    '--icon', str(ROOT / 'assets' / 'e6ticket.ico'),
                    '--distpath', str(DIST), '--workpath', str(BUILD / 'pyinstaller'),
                    '--specpath', str(BUILD), str(source)], cwd=ROOT, env=env, check=True)
    bundle = DIST / NAME
    if (bundle / 'data').exists():
        raise SystemExit('Refusing to distribute a bundle containing local application data.')
    for name in ('README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md'):
        shutil.copyfile(ROOT / name, bundle / name)
    licenses = bundle / 'licenses'
    licenses.mkdir(exist_ok=True)
    shutil.copyfile(Path(sys.base_prefix) / 'LICENSE.txt', licenses / 'Python.txt')
    for package in ('Pillow', 'PyInstaller'):
        dist = importlib.metadata.distribution(package)
        for file in dist.files:
            if '.dist-info/licenses/' in file.as_posix():
                target = licenses / package / Path(*file.parts[file.parts.index('licenses')+1:])
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(dist.locate_file(file), target)
    for name in ('Tcl.txt', 'Tk.txt'):
        shutil.copyfile(ROOT / 'licenses' / name, licenses / name)
    manifest = {
        'version': version, 'python': platform.python_version(), 'architecture': platform.machine(),
        'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
        'executable_sha256': hashlib.sha256((bundle / (NAME+'.exe')).read_bytes()).hexdigest(),
        'packages': {d.metadata['Name']: d.version for d in importlib.metadata.distributions()},
        'source_commit': os.environ.get('GITHUB_SHA', 'local-build'),
        'format': 'PyInstaller one-folder; no self-extraction to Windows Temp',
    }
    (bundle / 'BUILD_INFO.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    archive = DIST / f'{NAME}-{version}-windows-x64.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as output:
        for path in sorted(bundle.rglob('*')):
            if path.is_file():
                output.write(path, path.relative_to(DIST).as_posix())
    shutil.copyfile(source, DIST / 'ticket_bot.pyw')
    for algorithm in ('sha256', 'md5'):
        entries=[]
        for path, label in ((archive,archive.name),(DIST/'ticket_bot.pyw','ticket_bot.pyw'),
                            (bundle/(NAME+'.exe'), NAME+'/'+NAME+'.exe')):
            digest=hashlib.new(algorithm,path.read_bytes()).hexdigest()
            entries.append(f'{digest}  {label}')
        (DIST / (algorithm.upper()+'SUMS.txt')).write_text('\n'.join(entries)+'\n',encoding='utf-8')
    print('Release ready:', archive)


if __name__ == '__main__':
    main()
