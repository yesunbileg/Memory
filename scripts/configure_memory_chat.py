"""Save and verify the archive notebook's Custom chat rules; never print private text."""

import argparse
import asyncio
from pathlib import Path

from ai_memory.notebook import NotebookLMAdapter
from ai_memory.store import Store


async def configure(db):
    with Store(db, existing=True) as store:
        notebook = store.get_setting("notebook_id")
        storage = store.get_setting("storage_path")
    if not notebook:
        raise RuntimeError("Notebook is not configured")
    adapter = NotebookLMAdapter(notebook, Path(storage) if storage else None)
    try:
        await adapter.configure_memory_prompt()
    finally:
        await adapter.close()
    with Store(db, existing=True) as store:
        if (notebook, storage) != (
            store.get_setting("notebook_id"),
            store.get_setting("storage_path"),
        ):
            raise RuntimeError("Notebook settings changed; verification was not recorded")
        store.set_setting("memory_custom_prompt_verified", notebook)
    print("Custom chat rules verified; response length Default")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/conversations.sqlite"))
    asyncio.run(configure(parser.parse_args().db))
