#!/usr/bin/env python3
"""Install portable wrappers for macOS/Linux without copying credentials."""
import argparse
import os
from pathlib import Path
import shlex
import tempfile


def install(root, destination):
    root = Path(root).resolve()
    destination = Path(destination).expanduser().resolve()
    for filename in ('gpt-launch.mjs', 'launch.py'):
        if not (root / filename).is_file():
            raise FileNotFoundError(filename)
    destination.mkdir(parents=True, exist_ok=True)
    commands = {
        'gpt_claude': 'node ' + shlex.quote(str(root / 'gpt-launch.mjs')),
        'ai_claude': 'python3 ' + shlex.quote(str(root / 'launch.py')) + ' ai',
        'ds_claude': 'python3 ' + shlex.quote(str(root / 'launch.py')) + ' ds',
    }
    for name, command in commands.items():
        descriptor, temporary = tempfile.mkstemp(prefix='.' + name + '-', dir=destination)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as file:
                file.write('#!/bin/sh\nexec ' + command + ' "$@"\n')
            os.chmod(temporary, 0o755)
            # Replace a former wrapper/symlink, never overwrite its linked source.
            os.replace(temporary, destination / name)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return destination


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bin-dir', default=str(Path.home() / '.local' / 'bin'))
    args = parser.parse_args()
    destination = install(Path(__file__).parent, args.bin_dir)
    print('Installed gpt_claude, ai_claude and ds_claude into ' + str(destination))
    print('Add this directory to PATH, then configure providers using README.md.')
