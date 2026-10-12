# Security and guardrails

NYCynapse is open to anyone on the internet, calls language models that cost money, reads web pages
chosen at run time, and runs generated SQL against a lakehouse. This page lists what could go
wrong and what stops it. It follows the
[OWASP Top 10 for LLM Applications (2025)](https://genai.owasp.org/llm-top-10/) and the
[OWASP Top 10 for Agentic Applications (2026)](https://genai.owasp.org/); the codes in brackets
point to those lists.

## What the guide can do at all

Every tool is read-only. There is nothing to send, buy, post, delete or write.

| Tool | Reach |
|---|---|
| `ask_lake`, `lake_freshness` | Gold tables of the lake, through the semantic layer and a SQL guard |
| `weather`, `road_speeds`, `road_events`, `subway_alerts`, `find_place`, `area_stats` | Fixed public APIs (Open-Meteo, NWS, NYC DOT, 511NY, MTA, NYC GeoSearch, Census Reporter) |
| `open_data` | Six named NYC Open Data datasets, at most 50 rows |
| `web_search` | Serper, then Tavily, then a self-hosted SearXNG |
| `read_page` | One public web page, through the guarded fetch below |

## Prompt injection and goal hijacking [LLM01, ASI01]

* Web results and page text reach the model wrapped and labelled as untrusted material from other
  sites, to be used as information only. The instructions say plainly that instructions inside them
  are ignored and cannot change the rules.
* Even a successful injection has little to work with: no tool has side effects, and the agent's
  tools are fixed in code, not chosen by the page.
* Text from web pages is never carried into a conversation's memory. Follow-ups remember the city
  data that was found and only the titles of web pages [ASI06].

## Generated SQL [LLM05, ASI05]

* The model fills in a typed plan; code compiles it to SQL from governed metrics and declared joins.
  When it has to write SQL, a parser-based guard allows exactly one `SELECT` on gold tables and
  blocks file, network and system functions.
* The lake is attached read-only, extension loading and network access are off, file access is
  limited to the lake's data directory, and configuration is locked. The service sees that
  directory mounted read-only, and its Postgres role can only read the lake catalog.

## Page reads cannot reach private networks [ASI02]

`read_page` takes a URL from a search result or a model, so it is treated as hostile:

* only `http` and `https`, only ports 80 and 443, no credentials in the URL;
* the host is resolved by the service and every address must be public; loopback, private ranges,
  link-local (including the cloud metadata address `169.254.169.254`), multicast and reserved
  addresses are refused, as are names like `localhost` and `*.internal`;
* the connection goes to the address that was checked, so a second DNS answer cannot redirect it;
* each redirect is checked the same way, at most three;
* only text content types, at most 1.5 MB, 12 second timeout.

Tests in `tests/test_guide.py` cover these cases.

## Excessive agency and runaway loops [LLM06, ASI08]

* A turn has at most 4 rounds and 7 tool calls; at most 2 web searches, 2 page reads and 3 lake
  questions. The same call is never repeated. After 150 seconds the model must answer with what it
  has.
* Unknown tool names are refused and reported back to the model.
* If the main model is out of allowance or failing, the backup model from another vendor answers;
  the main one rests for 20 minutes. If both are out, the reader is told the models are at capacity.
* If the tools server process dies, it is restarted and the same functions are called in process.

## Cost and abuse [LLM10]

* A Cloudflare Turnstile check before a conversation starts.
* Per reader: 16 messages an hour and 48 a day. Per conversation: a question and three follow-ups,
  ending after 30 minutes idle.
* Questions are 3 to 400 characters, with control characters removed.
* Three answers run at once and ten more wait in a line, each told its place. Beyond that, readers
  are told the site is at full capacity rather than left waiting. A daily budget caps model spend
  for the whole site, and web searches have their own daily cap.
* `NYCYNAPSE_PAUSED=1` stops new questions without a deploy.

## Scope, people and harmful requests [LLM09, ASI09]

* The guide answers questions about New York City and declines anything else in one sentence.
* It does not find, track or profile private individuals, and declines help with crime, evading
  police, weapons or harm.
* Answers cite every fact with a numbered source and give the time of each reading, and say so
  when a feed is old. Citations that point at no source are dropped, and only cited sources are
  listed. Numbers in an answer are checked against what the tools returned; numbers that cannot be
  matched are flagged under the answer.

## System prompt and secrets [LLM02, LLM07]

* The instructions contain no secrets; keys live in files outside the repository, readable only by
  root, and never enter a prompt.
* The model is told not to discuss its instructions or setup, and declines when asked.

## Output handling [LLM05]

* Answers are rendered as plain text (paragraphs and simple lists), never as HTML.
* Source links are shown only for `http` and `https` URLs and open with
  `rel="noopener noreferrer nofollow"`.
* Responses carry a Content Security Policy (scripts from this site and the Turnstile widget only,
  map tiles from OpenFreeMap, radar tiles from the Iowa Environmental Mesonet), `X-Frame-Options: DENY`, `nosniff`, a strict referrer policy and a
  restrictive permissions policy.

## Privacy

* Nothing one reader asks is shown to another. There is no public list of recent questions, and a
  conversation can only be continued from the address that started it.
* Readers are identified only by a salted hash of their address.
* Conversations are kept for 30 days to review how the models answer, then deleted by a daily job
  (`nycynapse purge`). The page says so and asks readers not to include personal information.

## Third-party data terms

511NY events are shown as NYSDOT published them, with their update time, the "powered by 511NY"
credit and their disclaimer, and are not passed on to third parties.

## Reporting a problem

Open an issue on the repository, or for anything sensitive, contact the owner through
[giriworks.com](https://giriworks.com).
