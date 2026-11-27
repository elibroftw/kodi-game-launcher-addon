"""Atomic writes for the add-on's profile files."""

import json
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile


@contextmanager
def atomic_output(path, mode, **options):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(mode=mode, dir=path.parent, delete=False, **options) as handle:
            temporary = Path(handle.name)
            yield handle
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_json(path, data):
    with atomic_output(path, 'w', encoding='utf-8') as handle:
        json.dump(data, handle)


def write_bytes(path, data):
    with atomic_output(path, 'wb') as handle:
        handle.write(data)
