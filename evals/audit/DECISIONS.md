# Gold audit decisions

Every reference answer was reviewed by three models from three vendors (Claude Opus 5.5,
GPT-6-Astra and Gemini 3.1 Pro, each at its highest reasoning setting) against
[the conventions](../CONVENTIONS.md). Every case at least one of them flagged was then reviewed
by hand. The opinions are in `dev/` and `regression/`; holdout opinions are kept with the
private holdout. This file records what was decided for each flagged case and why.

| Case | Flagged by | Decision | Why |
|---|---|---|---|
| bk-004 | all three | Fixed, alternatives added | Williamsburg is three 2020 neighborhood areas; the reference used one. All three is the reference; one or two are accepted. |
| bk-007 | GPT, Gemini | Kept, grading rule added | Citi Bike does not run on Staten Island. Listing it with zero rides or leaving it out are the same answer. |
| bk-012 | GPT, Gemini | Alternatives added | Rides ending in New Jersey have no city borough; leaving them out or counting them either way are all reasonable. |
| cs-015 | Gemini | Kept, convention added | "Still open" means not closed. Now written in the conventions. |
| lv-001 | Gemini | Kept | The reference reads the freshness table as of the snapshot, which is what "right now" means for it. |
| lv-002 | Gemini | Kept | The time zone shown is formatting; results are compared as instants. |
| lv-004 | Gemini | Kept | "Full" already requires the station to accept returns, so removed stations are not counted. |
