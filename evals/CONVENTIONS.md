# Evaluation conventions

Every reference answer in the evaluation set follows these rules. They are also what the gold
audit checks against, and what the pipeline is built to do. Where a question could reasonably be
read more than one way, these rules decide which reading is the reference.

## Time

* **Now.** Each case has an `as_of` timestamp. "Now", "today", "this week" and so on are relative
  to it, not to when the evaluation runs.
* **Time zone.** Every period is New York local time. A day runs from midnight to midnight in New
  York. Timestamps stored as instants are filtered with New York boundaries.
* **Calendar periods.** "In June 2025" is the whole month. "In 2025" is the calendar year.
  A date is that whole day.
* **Relative periods.** Weeks run Monday to Sunday.
  * "Yesterday" is the previous calendar day.
  * "Last week", "last month" and "last year" are the previous whole week, month or year.
  * "This week", "this month" and "this year" run from the start of that week, month or year
    up to now.
  * "The last 7 days" is the 7 × 24 hours ending now.
  * "Since 1 March 2026" runs from that date up to now.
* **Named parts of the day**, all in New York time:

  | Name | Hours |
  |---|---|
  | Morning rush | weekdays 7:00 to 9:59 |
  | Evening rush | weekdays 16:00 to 18:59 |
  | Rush hour | both rush periods |
  | Business hours | weekdays 9:00 to 16:59 |
  | Overnight | 0:00 to 5:59 |
  | Morning | 6:00 to 11:59 |
  | Afternoon | 12:00 to 16:59 |
  | Evening | 17:00 to 21:59 |
  | Weekend | Saturday and Sunday |

* **Right now.** For status data (bike station availability), "right now" is the latest status of
  each station at or before `as_of`.

## What the data covers

* History starts on 1 January 2024. A question about an earlier period is declined, even if a
  source happens to carry a few older rows.
* A question about a period the data does not reach at all is declined, for example a month the
  city has not published yet, or a future date.
* A period the data covers only in part is answered with what is there, and the written answer
  says which dates are covered. It is not declined.
* Questions about things the lake does not hold (crime, population, restaurant inspections,
  forecasts of demand) are declined.

## Meaning

* **Governed metrics.** When a question asks for something the semantic layer defines as a
  metric, the reference uses that definition, including its built-in conditions, such as
  counting only plausible trips. A question can override a definition by asking explicitly.
* **App rides and taxis.** App rides are high-volume for-hire trips (Uber, Lyft and the like).
  Taxi trips are yellow and green taxis.
* **Places.**
  * A neighborhood is a 2020 Neighborhood Tabulation Area.
  * A common name that covers several of them ("Washington Heights") means all of them added
    together, unless the question asks to compare them.
  * A borough is the borough recorded on the row itself.
  * A ZIP code is the ZIP recorded on the row, not the health department's ZIP code
    tabulation area.
  * A named landmark ("near Yankee Stadium") means the areas listed for it in
    `semantic/places.yaml`.
* **Status words.** A 311 request that is "open" or "still open" is one that is not closed,
  whatever its stage (open, assigned, in progress).
* **Subway.**
  * Ridership counts the subway only, unless the question names the Staten Island Railway or
    the Roosevelt Island Tram.
  * Delay is arrival time minus scheduled time, so a positive delay is late.
  * "Late" means more than five minutes behind schedule.

## Comparing answers

* An answer is right when its rows match the reference rows.
* **Row order and extra columns.** Row order counts only when the question asks for a ranking
  or an order. Extra columns are allowed unless the case says otherwise.
* **Numbers.** Numbers must match within the case's tolerance. A share may be given as a
  fraction or as a percentage.
* **Zero groups.** A group listed with zero (Staten Island with no Citi Bike rides) is the same
  answer as leaving the group out.
* **Alternative readings.** When a question genuinely allows more than one reading and these
  rules do not settle it, the case lists each reading as an accepted answer. An answer matching
  any of them is right.
* **Declines.** For a question that should be declined, the answer is right when the system
  declines. For one that needs clarifying, it is right when the system asks.
