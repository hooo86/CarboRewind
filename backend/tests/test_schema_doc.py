import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import gen_schema


def test_schema_doc_is_up_to_date() -> None:
    assert gen_schema.main(["--check"]) == 0, "run: python tools/gen_schema.py"
