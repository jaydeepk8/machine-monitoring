# Machine Monitoring

CNC/VMC machine monitoring — FOCAS data collection, state detection,
downtime analysis and OEE.

## Layout

- `operations/` — monitoring logic, one file per operation
- `analysis/` — exploration scripts
- `data/` — generated datasets (gitignored)
- `lib/` — FOCAS DLLs (gitignored, see Setup)

## Setup

Two environments are needed. The FOCAS DLLs are 32-bit, so the collector
runs on 32-bit Python; everything else runs on 64-bit.

    python -m venv venv32    # 32-bit Python, collector only
    python -m venv venv64    # 64-bit Python, analysis and operations

`lib/` is not in this repo. The FOCAS libraries are licensed by FANUC and
must be obtained from FANUC directly.

Install the packages for the 64-bit environment with:

    pip install -r requirements.txt

## Settings a client must give

Every shift must list its breaks, even if there are none. Breaks change the
OEE number, so there is no default.

    {"name": "Shift 1", "start": "07:00", "end": "15:00",
     "breaks": [{"start": "12:30", "end": "13:00"}]}

`weekly_off` lists the days the shop is closed, for example `["Sunday"]`.

In the machines file, `capabilities` says what a machine can report
(`part_count`, `cycle_time`, `cutting_hours`). What it cannot report shows as
"no data", never as a wrong number. An optional top-level `network_check` gives
the address of the shop's switch or router, so a network failure is not counted
as machines being down. Without it, the collector assumes a network problem
only when every machine goes silent together.

## How the numbers treat time

Live numbers cover the current working day, which starts with the first shift.
Readings stay in the database. MTBF and the maintenance rate use the last 30 days.

| State | Meaning | Counted as lost time |
|---|---|---|
| `PLANNED_STOP` | a break, a weekly off, or outside the shifts | no |
| `NO_DATA` | the box or the network was down, so nothing is known | no |
| `NOT_RESPONDING` | the box was fine, but the machine did not answer | yes |

## Asking the shop questions (AI)

`assistant/` lets staff ask about the machines in their own words, in English,
Marathi or Hindi:

    python assistant/ask.py "Why was V61 slow today?" --lang mr

How it stays honest:

- Claude can only use the fixed, read-only lookups in `assistant/tools.py`. It
  never writes database queries, and all maths is done in code.
- Every number in an answer is checked against what the lookups returned. A
  number that is not in the data is printed as a warning.
- When the data cannot answer a question (yesterday, energy, operators, orders),
  it says so instead of guessing.

It needs internet and an Anthropic API key in `ANTHROPIC_API_KEY`. Everything
else in this repo works offline. It reads `output/results.db`, so run
`run_all.py` first. The model is `claude-opus-5-5`; a declined request is retried
on Anthropic's recommended fallback model.

### The test set

`assistant/testset.json` holds 33 questions with known answers for the simulated
day: 27 the AI must answer exactly, and 6 it must decline. Eight are also written
in Marathi and Hindi. Run it after every prompt or model change:

    python assistant/evaluate.py              # English
    python assistant/evaluate.py --lang mr    # the Marathi questions

Each run calls the API and costs money. Results are saved in `output/evals/`.
`python -m pytest` checks, without the API, that every expected answer is still
in the data.

## Running the tests

    python -m pytest

## Generating test data

    python cnc_simulator.py --hours 8 --machines 3 --out data/cnc_sim_data.csv
