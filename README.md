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

## The end-of-shift WhatsApp message

When a shift's report is written, a short message for each person in the
settings file is queued with it, in their language (`en`, `mr` or `hi`). It says
how much (parts, OEE of each machine) and why (what cost the lowest machine
most, the longest stop), then what to look at: slow programs, maintenance due,
stops with no reason tapped, and missing readings. Every number is copied from
the result tables. This part does not use AI.

    "whatsapp": {
      "provider": "file",
      "recipients": [{"name": "Owner", "phone": "+919812345678", "lang": "mr"}]
    }

- `provider: "file"` writes each message to `output/messages/` instead of
  sending it. Use it to try things out.
- `provider: "meta"` sends through Meta's official WhatsApp Cloud API, with the
  token in `WHATSAPP_TOKEN`. This is the one to use with shops. Testing on
  Meta's test number is free. Real messages are charged per delivered message
  (a utility message in India was about ₹0.12 in late 2026), roughly ₹20 a month
  for three shifts to two people.
- `provider: "wasender"` sends through [WasenderAPI](https://wasenderapi.com),
  with the key in `WASENDER_API_KEY`. It costs $6 a month per WhatsApp number
  after a 3-day trial (the trial allows 1 message a minute, 50 a day). It is not
  Meta's official API: it works through a WhatsApp number linked by QR code, so
  WhatsApp can restrict or ban that number. Use a separate SIM, not the owner's
  own number, and only message people who agreed to it.

`auto_run.py` sends what is waiting, one message every 6 seconds. If the
internet is down, a message waits and is tried again later, up to 6 times.

    python operations/shift_message.py --show --lang mr       preview, sends nothing
    python operations/shift_message.py                        the outbox
    python operations/shift_message.py --retry-failed         try failed ones again
    python operations/shift_message.py --test +919812345678   one test message
    python operations/shift_message.py --template             the Meta templates to submit

### Meta's API: how a message is sent

WhatsApp only lets a business start a chat with a template that Meta has
approved. A value filled into a template cannot hold a line break, so the
layout is in the template and each of its 6 values is one line: the shift, the
shop, parts made, OEE by machine, why, and what to look at. If a long list would
take the message over Meta's 1024 character limit, the last items are left out
and the message says how many. `--template` prints the template for each
language with sample values from the latest shift, ready to paste.

### Trying it on Meta's test number

1. At developers.facebook.com, create an app and add **WhatsApp**. Meta gives
   it a free test number and a test WhatsApp Business account.
2. In **WhatsApp > API Setup**, copy the **Phone number ID**. Under **To**, add
   your own phone and enter the code WhatsApp sends you. The test number only
   sends to phones added here.
3. Press **Generate access token** and set it in the terminal, not in a file:

       $env:WHATSAPP_TOKEN = "EAAG..."          # PowerShell

   This token expires quickly. For the edge box, make a System User token with
   `whatsapp_business_messaging` (Business settings > System users).
4. Make a settings file in `settings/clients/` with:

       "whatsapp": {
         "provider": "meta",
         "meta_phone_number_id": "<the Phone number ID>",
         "recipients": [{"name": "Me", "phone": "+91<your number>", "lang": "mr"}]
       }

5. `python operations/shift_message.py --settings settings/clients/<file> --test +91<your number>`
   sends Meta's ready-made `hello_world` template. If that arrives, the token,
   the number ID and your phone are right.
6. In **WhatsApp Manager > Message templates**, create a template named
   `shift_report`, category **Utility**, language **English**. Paste the English
   body and samples from `--template` (run `python operations/run_all.py` first
   if it says there are no results). Add **Marathi** and **Hindi** to the same
   template the same way. Wait for each to be approved.
7. Send the simulated day:

       python operations/auto_run.py --settings settings/clients/<file> --source csv --times 1 --fresh

   `--fresh` forgets earlier reports and messages, so the day is sent again.
   `python operations/shift_message.py --settings settings/clients/<file>` shows
   whether it went, and Meta's error with a hint if not.

Phone numbers are personal. Keep real client settings in `settings/clients/`,
which git ignores. The Marathi and Hindi wording is a first draft, to be checked
by a native speaker.

## Running the tests

    python -m pytest

## Generating test data

    python cnc_simulator.py --hours 8 --machines 3 --out data/cnc_sim_data.csv
