"""A1: `Keepfold`'s tools live in `keepfold.mcp_tools` mixins. The split is a pure move: the
`tools/list` a client reads stays byte-identical to the snapshot taken before it, and every name
`dir(Keepfold)` had before it still resolves. Snapshots: tests/fixtures/mcp_split/."""

import json
from pathlib import Path

import anyio
import pytest
from mcp import Client

from keepfold.mcp_server import Keepfold, build_server
from keepfold.vault import Vault

pytestmark = pytest.mark.usefixtures("home")

SNAP = Path(__file__).parent / "fixtures" / "mcp_split"


def tools_list(root) -> str:
    """The tools/list result as a client receives it, as stable JSON."""
    async def main():
        async with Client(build_server(Vault(root))) as c:
            return (await c.list_tools()).model_dump(mode="json", by_alias=True)

    return json.dumps(anyio.run(main), indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def test_tools_list_byte_identical(root):
    assert tools_list(root) == (SNAP / "tools_list.json").read_text(encoding="utf-8")


def test_old_dir_is_a_subset():
    old = set(json.loads((SNAP / "keepfold_dir.json").read_text(encoding="utf-8")))
    assert not old - set(dir(Keepfold))
