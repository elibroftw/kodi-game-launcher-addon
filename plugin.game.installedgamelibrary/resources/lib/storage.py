"""Atomic writes for the add-on's profile files."""

import json
from pathlib import Path
from tempfile import NamedTemporaryFile


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(
            mode='w', encoding='utf-8', dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(data, handle, ensure_ascii=True)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
