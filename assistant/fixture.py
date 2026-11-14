# the fixed shop day the test set is checked against
# built from the simulator CSV, so every expected answer is a known number

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "operations"))

import pandas as pd  # noqa: E402

from run_all import calculate  # noqa: E402
from settings import load_settings  # noqa: E402
from store import save_tables  # noqa: E402

SETTINGS = ROOT / "settings" / "example_client.json"
DATA = ROOT / "data" / "cnc_sim_data.csv"


def build(db_path, settings_path=SETTINGS, data_path=DATA):
    s = load_settings(settings_path)
    df = pd.read_csv(data_path, low_memory=False)
    tables, _ = calculate(df, s)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    save_tables(str(db_path), tables)
    return db_path


if __name__ == "__main__":
    print("saved:", build(ROOT / "output" / "fixture.db"))
