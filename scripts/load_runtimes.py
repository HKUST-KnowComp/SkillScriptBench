"""Verify split runtime assets, then optionally stream them into Docker."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess


def verify(manifest):
    manifest = Path(manifest).resolve()
    data = json.loads(manifest.read_text())
    if data.get('schema') != 'skillscriptbench-runtime-parts-v1':
        raise ValueError('unsupported runtime asset manifest')
    paths, names, total, combined = [], set(), 0, hashlib.sha256()
    for item in data['parts']:
        name = item['file']
        if Path(name).name != name or name in names:
            raise ValueError('invalid or duplicate part name')
        names.add(name)
        path = manifest.parent / name
        if path.is_symlink() or path.stat().st_size != item['bytes']:
            raise ValueError('runtime part missing or incorrect size: ' + name)
        digest = hashlib.sha256()
        with path.open('rb') as source:
            for chunk in iter(lambda: source.read(4 * 1024 * 1024), b''):
                digest.update(chunk)
                combined.update(chunk)
                total += len(chunk)
        if digest.hexdigest() != item['sha256']:
            raise ValueError('runtime part changed: ' + name)
        paths.append(path)
    if total != data['archive_bytes'] or combined.hexdigest() != data['archive_sha256']:
        raise ValueError('combined runtime archive mismatch')
    return data, paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--load', action='store_true', help='import verified assets into the Docker engine')
    args = parser.parse_args()
    data, paths = verify(args.manifest)
    print(json.dumps({'status': 'verified', 'parts': len(paths), 'bytes': data['archive_bytes']}), flush=True)
    if not args.load:
        return
    with subprocess.Popen(['docker', 'image', 'load'], stdin=subprocess.PIPE) as proc:
        try:
            for path in paths:
                with path.open('rb') as source:
                    for chunk in iter(lambda: source.read(4 * 1024 * 1024), b''):
                        proc.stdin.write(chunk)
            proc.stdin.close()
            if proc.wait():
                raise RuntimeError('Docker could not load the archive')
        except BaseException:
            if proc.poll() is None:
                proc.terminate()
            raise
    for image in data['images']:
        observed = subprocess.check_output(['docker', 'image', 'inspect', image, '--format', '{{.Id}}'], text=True).strip()
        if observed != image:
            raise ValueError('loaded image identity mismatch')
    print(json.dumps({'status': 'loaded', 'images': len(data['images'])}))


if __name__ == '__main__':
    main()
