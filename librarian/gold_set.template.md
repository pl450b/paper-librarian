# Gold set: does the triage model agree with you?

An optional calibration check, worth running before a large backfill. Copy
this file to `gold_set.md`, list 10-20 papers you know well (by library id),
and rate each one **before** running anything, using the rubric in
`brief.md`:

- **3**: core: read in full
- **2**: supporting: read in full, shorter note
- **1**: background: abstract summary only
- **0**: off-topic

Put a single digit in the "You" column. Then run:

    librarian/.venv/bin/python -m librarian gold

It triages each paper and writes `gold_report.md`. The check passes if all
but two ratings agree within ±1 and no paper you rated 3 was scored 0-1. If
it fails, read the model's reasons in the report and sharpen the rubric in
`brief.md`. Find paper ids with `python -m librarian find <title words>`.

| id | Paper | You |
|---|---|---|
| 1 | (title, for your reference) |  |
