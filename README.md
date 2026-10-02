# NYCynapse.Ai

Ask questions about New York City in plain English and get answers from live city data, with the SQL and the sources behind them. The data comes from [NYCynapse Lake](https://github.com/giridhar1103/NYCynapse_Lake): taxi and app rides, the subway, Citi Bike, 311, crashes, road speeds and weather, from January 2024 to the last few minutes.

The design follows the path Uber described for [QueryGPT](https://www.uber.com/blog/query-gpt/) and the semantic layers that came after it. A question is never answered by handing a model the whole schema. It is narrowed step by step: which domain, which tables, which values, which joins, and only then SQL, checked before and after it runs.

**Status:** the semantic layer and its catalog are built. The query pipeline, evaluation set and web interface come next.

## Semantic layer

Everything the system may know about the lake is declared in [`semantic/`](semantic):

| File | What it holds |
|---|---|
| `workspaces.yaml` | Ten domains (taxis and app rides, subway, Citi Bike, 311, traffic safety, road speeds, weather, data freshness, places, calendar) and the models in each |
| `models/` | 27 semantic models over the gold tables: time columns, keys, dimensions, measures and named filters |
| `metrics/` | 39 governed metrics with units, bounds and how they may be added up |
| `relationships.yaml` | 46 joins the planner is allowed to use |
| `time.yaml` | Named parts of the day and week, such as rush hour |
| `places.yaml` | Landmarks, venues, airports and stations people name, such as Yankee Stadium |
| `instructions.yaml` | Domain rules, such as how subway delay is signed or why taxi tips need card trips |

A few choices worth calling out:

* **Metrics carry their additivity.** Precipitation adds up over hours but not across boroughs, so a citywide figure averages the boroughs instead of summing five weather stations. The compiler enforces this instead of trusting generated SQL to remember it.
* **Metrics can have several sources.** Trip counts come from the hourly zone aggregate when the question allows it, and fall back to the 650 million row trip table only when it needs something the aggregate does not carry.
* **Joins are declared, not invented.** Relationships include plain key joins, time joins (a crash to the weather in its borough that hour) and range joins (a subway arrival during a weather alert). Range joins can match more than one row, so they are applied as `EXISTS` filters and never inflate counts.
* **Places resolve before SQL is written.** "Near Yankee Stadium" becomes a fixed set of neighborhoods, taxi zones, subway stations and bike stations, worked out from the lake's boundaries when the catalog is built.

`semantic/gold_manifest.json` is a snapshot of the gold tables and their dbt documentation. `nycynapse check` parses every SQL fragment in the semantic layer with sqlglot and checks every column it names against that snapshot, so a renamed column in the lake fails CI here.

## Catalog

`nycynapse publish` validates the semantic layer and writes it to Postgres as a new catalog version:

* 372 objects (models, dimensions, metrics, relationships, places and so on), each with a short description, a 384-dimension embedding (`bge-small-en-v1.5`, run locally) and a full text index
* 16 thousand distinct values of the dimensions that need grounding, such as complaint types, station names and neighborhood names, indexed with trigrams so "loud parties" finds `Loud Music/Party`
* the coverage of every named place

A version only becomes current once all of it is written. Older versions are kept because traces and evaluation runs record the version they used.

## Read-only access to the lake

Generated SQL runs on a DuckDB connection with DuckLake attached read-only, network access and extension loading off, file access limited to the lake's data directory, and configuration locked. File access cannot be switched off completely, because DuckLake reads its own Parquet files, and DuckDB still allows `COPY ... TO` inside an allowed directory. So the query pipeline adds two more layers: a SQL guard that only lets a single `SELECT` through, and a query worker running as a user that cannot write to the lake.

## Running it

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
set -a; . /path/to/env; set +a        # NYC_LAKE_PG_DSN, NYC_APP_PG_DSN

.venv/bin/nycynapse export-gold      # refresh the gold snapshot from the lake
.venv/bin/nycynapse check            # validate the semantic layer
.venv/bin/nycynapse publish          # write a new catalog version
.venv/bin/pytest -q
```
