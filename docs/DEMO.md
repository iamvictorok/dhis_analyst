# DHIS2 Analyst: live demo script

A step-by-step script for presenting the DHIS2 Analyst. Follow it top to bottom: the checklist, the opening, the demo steps in order, then the closing. The questions and figures here have been checked against the DHIS2 Analytics API.

> **What this is:** a working build running against the public DHIS2 demo database (Sierra Leone), hosted on a free Render instance for demos. It is **not** a production deployment for any client. Say "build" or "demo", never "in production".

- **Hosted app:** https://dhis2-analyst.onrender.com
- **Data:** public DHIS2 play instances (`play.im.dhis2.org`), with 2.41.10 as primary and 2.40.12 and 2.42.7 as fallbacks
- **Model:** Google Gemini Flash (`gemini-flash-latest`), with automatic fallback to Gemini Flash-Lite
- **Where the figures come from:** QA runs on 21 Oct 2026. Each figure was checked by script against `/api/analytics` on stable-2-41-10. Every step says whether it was checked **on the hosted app** or **locally only**.
- **Which build was tested:** the hosted checks ran on the Render app across successive commits of branch `audit/demo-fixes-2026-10-21`, from `06db8cd` through `72e3d9a` (the map build). The automated suite (276 tests) passes on `72e3d9a`. To save Gemini quota, not every question was re-run on the final commit, so **rehearse your chosen path on the build you will demo**.

---

## Contents

1. [Pre-demo checklist](#1-pre-demo-checklist)
2. [Opening (about 1 minute)](#2-opening-about-1-minute)
3. [The demo flow](#3-the-demo-flow)
4. [Handling questions from the audience](#4-handling-questions-from-the-audience)
5. [Recovery](#5-recovery)
6. [Avoid on stage](#6-avoid-on-stage)
7. [Closing and call to action](#7-closing-and-call-to-action)
8. [Appendix: verified figures at a glance](#appendix-verified-figures-at-a-glance)

---

## 1. Pre-demo checklist

### The day before
- [ ] Rehearse the **short path** once (Steps 1–4, 15, 17, 18, 19). Don't run the whole script the morning of the demo. Each question makes several Gemini calls, and the primary model's free quota is tight.
- [ ] Download the backup files (Report PDF/DOCX, Excel export, map PNG) from the rehearsal and keep them on the laptop.
- [ ] Note the Gemini quota reset. It is around **08:00 WAT**, so a 09:00 demo starts on a fresh quota.

### T-30 minutes
1. Open `https://dhis2-analyst.onrender.com/health` in a browser tab.
   - The free instance sleeps after about 15 minutes idle. The first load can take **about 1 minute**, plus about 25 s for the DHIS2 metadata sync.
   - Expected response shape:
     ```json
     {"status":"ok","deployment_mode":"standalone","llm_provider":"gemini",
      "dhis2":{"mode":"live","snapshot_mode":"auto"},
      "build":"map-2026-10-21.1","commit":"<7-char SHA>"}
     ```
   - Check that `commit` matches the latest commit on the branch you are demoing, that `dhis2.mode` is `live`, and that `llm_provider` is `gemini`.
2. Open `https://dhis2-analyst.onrender.com/api/dhis2/status`. Check that all **3 instances** show `"ok": true`:
   - `stable-2-41-10` → `2.41.10` (primary)
   - `stable-2-40-12` → `2.40.12`
   - `stable-2-42-7` → `2.42.7`

   If the primary is down, the app moves to the next instance by itself. See [Recovery](#5-recovery).

### T-10 minutes
3. Open the app and **enter the access code you were given**. The code is never written in this repository.
4. Click **one chip** (the *ANC 1 Coverage · Sierra Leone · 12 months* chip is the quickest) to warm the model and the DHIS2 connection. Leave that answer on screen or start a new chat.
5. Check the **status pill** in the header. It should read **Live** with a green dot.
6. Set expectations in your own head:
   - A normal answer takes **8–20 s**.
   - If the primary Gemini model is out of quota or overloaded, the backup model answers, and a question takes **about 30–35 s**.
   - The spinner shows until the whole answer is ready. Nothing streams in part-way.
   - Talk through the wait. The talking points below are sized for it.

### T-2 minutes
7. **Browser:** full screen (F11). Zoom to **110–125%** so the back row can read the "Data used" line. Close other tabs and notifications.
8. **Projector:** light theme and high contrast. Map titles are 18 px and legends 13 px, designed for a projector at normal zoom.
9. **Backup tab:** a second tab already signed in, with the backup files from the rehearsal one click away.
10. **Don't leave the app idle more than about 10 minutes** before you start. If you have to wait, click a chip again at about T-5.

---

## 2. Opening (about 1 minute)

Say this calmly, in your own words:

> "Every district health office has DHIS2, and the data in it is good. The trouble is getting answers out of it. If a district manager wants to know *which chiefdoms are behind on BCG this year*, they either learn pivot tables and the analytics app, or they send a request to the one data officer and wait.
>
> This is a working build of an assistant that sits on top of DHIS2. You ask in plain English. It finds the right data element or indicator in *your* instance, pulls the figures from the DHIS2 Analytics API, and gives you back a chart, a table, a map, a report or an Excel file. Every answer says exactly which DHIS2 item, place and period it used, so you can check it.
>
> It's read-only. It never changes your data. And when something isn't in your DHIS2, it says so instead of guessing.
>
> Everything you'll see is live against the public DHIS2 demo database for Sierra Leone."

---

## 3. The demo flow

**How to use this section**
- The steps run from safe to impressive. **Core** steps make up a 10–12 minute demo. **Optional** steps stretch it to about 25 minutes.
- **Type** means type the question into the box. You can copy the text from the code block. **Click** means click the suggested-question chip.
- **Mode** is the dropdown to the left of the input box: **Auto · Answer · Dashboard · Report · Slides · Export · Map**. Leave it on **Auto** unless the step says otherwise. Chips set their own mode.
- **Time** is the wait for the answer plus your talking.
- "Last 12 months" and "this year" figures were recorded on **21 Oct 2026** and can move a little as DHIS2 data updates. Figures for 2024 and 2025 are fixed.

### Short path (10–12 minutes)
Steps 1 → 2 → 3 → 4 → 15 → 16 → 17 → 18 → 19 → close.

### Full path (about 25 minutes)
Every step, in order.

---

### Act 1: The basics, one click away

#### Step 1. Sign in and show the status pill (Core · 30 s)
- **Do:** if you are not already signed in, enter the access code you were given.
- **Point at:**
  - the **status pill** in the header (**Live**, green). Hover it to show "Live DHIS2: https://play.im.dhis2.org/stable-2-41-10";
  - the **suggested-question chips** on the welcome screen.
- **Talking point:** "The access code keeps the demo private. The dot tells you which DHIS2 the answers come from: Live, Fallback, Cached or Offline. I'll come back to that."

#### Step 2. ANC 1 coverage, last 12 months (Core · 30–45 s) ✅ hosted
- **Click:** chip **ANC 1 Coverage · Sierra Leone · 12 months**
  ```
  Show me ANC 1 Coverage in Sierra Leone for the last 12 months
  ```
- **Point at:**
  1. The **"Data used" line** at the top of the answer: `ANC 1 Coverage (indicator Uvn6LCg7dVU) · Org unit … · Period: LAST_12_MONTHS → October 2025 … September 2026`.
  2. The chart, with 12 monthly values.
  3. The chart toolbar (top right of the chart). The **camera icon** downloads the chart as a PNG.
- **Expected (as of 21 Oct 2026):** 12 values (12/12 matched). Peak **May 2026 = 139.39**, low **Dec 2025 = 79.37**.
- **Talking point:** "I didn't pick an indicator from a list or build a pivot table. It found ANC 1 Coverage in this instance and shows the exact UID and period, so a data officer can check it in the Data Visualizer in 10 seconds."

#### Step 3. Penta 3 by district, 2025 (Core · 30 s) ✅ hosted
- **Click:** chip **Penta 3 coverage <1y by district · 2025**
  ```
  Show Penta 3 coverage <1y for each district in Sierra Leone for 2025
  ```
- **Point at:** the 13 district bars, and the "Data used" line saying "broken down by District (LEVEL-2)".
- **Expected:** 13/13 districts matched. Highest **Western Area 66.49**, then Bo 31.49. Lowest **Kono 10.88**.
- **Talking point:** "Same question style, now split across every district. It used the instance's own level name, *District*, rather than anything hard-coded."

#### Step 4. IDSR malaria trend (Core · 30 s) ✅ hosted
- **Click:** chip **IDSR Malaria trend · 12 months**
  ```
  Show me the trend of IDSR Malaria in Sierra Leone for the last 12 months
  ```
- **Point at:** the line chart. There are **11 points, not 12**.
- **Expected:** 11/11 matched. **April 2026 has no data** in DHIS2. Peak **June 2026 = 1,974** cases.
- **Talking point:** "Notice there's a gap. April has no data in DHIS2, so it shows 11 months. It doesn't fill the gap or make a number up."

---

### Act 2: Ask it the way a manager would

#### Step 5. A plain count (Optional · 30 s) ✅ hosted
- **Type** (Mode: Auto):
  ```
  How many ANC 1st visits were recorded in Sierra Leone in 2025?
  ```
- **Expected:** a short written answer: **253,269**, with "Source: DHIS2 Analytics" naming the data element.
- **Talking point:** "When the question wants one number, you get one number in a sentence. You don't get a chart you didn't ask for."

#### Step 6. Count vs coverage (Optional · 30 s) ✅ hosted
- **Type:**
  ```
  Fully immunised children by district this year
  ```
- **Point at:** the "Data used" line. It names **Fully Immunized child (data element `UOlfIjgN8X6`)**, a count, not the FIC coverage indicator.
- **Expected (2026 to date, as of 21 Oct):** 13 districts. Highest **Western Area 15,751**, then Kenema 5,176.
- **Talking point:** "'Children' means a count, so it picks the data element. If you say 'coverage' or '%', it picks the indicator. It also understands the British spelling, *immunised*, against DHIS2's American *Immunized*."

#### Step 7. Down to chiefdom level (Optional · 30 s) ✅ hosted
- **Type:**
  ```
  How many BCG doses were given by chiefdom in Bo district in 2025?
  ```
- **Point at:** "Bo … broken down by Chiefdom (LEVEL-3)".
- **Expected:** **15 chiefdoms**, 15/15 matched. Highest **Badjia 2,116**, then Kakua 1,231 and Tikonko 1,178. Lowest **Selenga 96**.

#### Step 8. Down to a single facility (Optional · 20 s) ✅ hosted
- **Type:**
  ```
  ANC 1st visits at Ngelehun CHC in 2025
  ```
- **Expected:** **913**, at facility level (`DiszpKrYNg8`).
- **Talking point:** "Country, district, chiefdom, facility: the same plain question at every level."

#### Step 9. Monthly trend within one year (Optional · 30 s) ✅ hosted
- **Type** (set **Mode: Dashboard**):
  ```
  Monthly Penta 3 coverage for Bo in 2025 as a line chart
  ```
- **Point at:** "Period: 202501 … 202512 (12 periods)".
- **Expected:** 12/12 matched. **Jan 20.65**, peak **Oct 63.70**, Dec 18.96.
- **Talking point:** "'Monthly' plus a year becomes the 12 months of that year. It also handles 'quarterly' and 'weekly'."

#### Step 10. Age and other breakdowns (Optional · 30 s) ✅ hosted
- **Type:**
  ```
  BCG doses given in Bo in 2025 broken down by age group
  ```
- **Expected:** "Disaggregated by: EPI/nutrition age". **<1y = 7,941**, **>1y = 1,024**.
- **Alternative (also verified):**
  ```
  Measles doses given in Sierra Leone in 2025 split by fixed and outreach
  ```
  Expected: **Fixed 47,151**, **Outreach 18,265**.
- **Talking point:** "Category option breakdowns, like age or fixed vs outreach, come straight from the DHIS2 category combos."

#### Step 11. Reporting timeliness (Optional · 20 s) ✅ hosted
- **Type:**
  ```
  How timely was Child Health reporting in Bo in 2025?
  ```
- **Point at:** "Child Health - Reporting rate on time (reporting rate `BfMAe6Itzgt.REPORTING_RATE_ON_TIME`)".
- **Expected:** **80.69%**.
- **For completeness instead:**
  ```
  What was the reporting rate of the Child Health data set by district last quarter?
  ```
  Expected: 13 districts, Jul–Sep 2026. Highest **Bombali 98.61**, lowest **Kono 72.16**. Checked on the hosted app; figures as of 21 Oct.
- **Talking point:** "Data quality questions, like how complete and how timely reporting is, use DHIS2's own reporting-rate metrics."

#### Step 12. Tracker / programme indicator (Optional · 30 s) ✅ hosted
- **Type:**
  ```
  From the Child Programme tracker, how many BCG doses were recorded nationally in 2025?
  ```
- **Point at:** "BCG doses (program indicator `p2Zxg0wcPQ3`)".
- **Expected:** **5,503**.
- **Talking point:** "Name a tracker programme and it uses that programme's indicator, aggregated through Analytics. It doesn't pull individual records."

#### Step 13. A year-on-year comparison (Optional · 20 s) ✅ hosted
- **Type:**
  ```
  Compare Measles doses given in Bo in 2024 versus 2025
  ```
- **Expected:** **7,344 → 6,849** (−6.7%).

---

### Act 3: Why you can trust it

#### Step 14. A vague question gets a clarifying question (Optional, strongly recommended · 20 s) ✅ hosted
- **Type:**
  ```
  Show me coverage
  ```
- **Expected:** no chart. Instead: *"'coverage' could mean several different measures in this DHIS2 instance. Which one do you want?"*, followed by a numbered list of indicators from this instance, such as BCG, ANC 3, Measles <1y, OPV 3 and Penta 3.
- **Optional follow-on:** reply with one of the names or numbers.
  - This exact reply was **not** QA'd on stage, so keep it simple, for example `Penta 3 Coverage <1y`.
  - If it asks for a period or place, give one.
- **Talking point:** "It asks instead of guessing. A wrong chart that looks right is the worst thing an analytics tool can give a manager."

#### Step 15. Honest "not available" (Core · 20 s) ✅ hosted
- **Type:**
  ```
  Show me COVID-19 booster vaccine doses in Bo last year
  ```
- **Expected:** *"**COVID-19 booster vaccine doses** isn't available in this DHIS2 instance: no data element, indicator or program indicator here measures it, so I haven't substituted anything else."*
  - It then lists the closest items that **do** exist, such as Penta3 doses given, OPV3 doses given and Measles doses.
- **Talking point:** "An earlier version quietly answered this with a different vaccine. We fixed that. If it's not in your DHIS2, it tells you, and shows you what *is* there."

#### Step 16. Show the working: numerator and denominator (Core · 30 s) ✅ hosted
- **Type:**
  ```
  Give me the numerator and denominator behind ANC 1 Coverage for Sierra Leone in 2025
  ```
- **Expected:**
  - Formula: numerator = **Total ANC 1 visits**; denominator = **Estimated number of pregnant women** (per cent, factor 100).
  - Value: **103.82 = 253,269 / 243,951**.
  - It may show as a short report.
- **Talking point:** "Coverage over 100% always gets a question. Here's why: the denominator is DHIS2's population estimate. The tool shows you DHIS2's own formula. It doesn't hide it."

---

### Act 4: Maps, reports, exports

#### Step 17. District choropleth map (Core · 45–60 s) ✅ hosted
- **Type** (Mode: Auto; the word "map" is enough):
  ```
  Map of ANC 1 coverage by district, Sierra Leone, 2025
  ```
- **Expected:**
  - Title **"ANC 1 Coverage by district, Sierra Leone, 2025"**.
  - 13/13 districts match DHIS2 exactly.
  - Summary line: **"Highest: Bo (146.7%) · Lowest: Kono (53.8%) · 13 districts on the map"**.
  - Legend: 5 classes, from 53.8% to 146.7%.
- **Point at:**
  1. Hover a district to show its name and value.
  2. The legend.
  3. The **"Data used"** line above the map.
  4. The full **table of values** under the map.
  5. The **PNG**, **XLSX** and **CSV** buttons under the map.
- **Do:** click **PNG**. A 1400×900 image with tiles, legend and title downloads.
- **Talking point:** "The boundaries come from DHIS2 itself, the instance's own geometry, not a shapefile we bundled. Point it at another country's DHIS2 and it draws that country."
- **Time note:** this was the slowest hosted call (35 s on the backup model). Keep talking while it loads.

#### Step 18. Programme review report → PDF / DOCX (Core · 45 s) ✅ hosted
- **Click:** chip **Programme review report · ANC 1**
  ```
  Write a programme review report on ANC 1 Coverage in Sierra Leone for the last 12 months
  ```
- **Point at:**
  - the structured report;
  - its figures (mean **102.41**, change **+26.6%** over the window, as of 21 Oct);
  - the **DOCX** and **PDF** buttons.
- **Do:** click **PDF** (or DOCX) and open the file.
- **Expected:** in the hosted test, the DOCX opened with 18 paragraphs and the PDF was valid text. The report's mean, peak, low and % change were checked against recomputed values. No invented figures were found.
- **Talking point:** "A first draft of the quarterly review, with the numbers already checked against DHIS2. The analyst edits the words, not the arithmetic."
- ⚠️ The report box says **Editable**, but **edits made in the browser are not carried into the download**. Don't edit and then download on stage.

#### Step 19. Excel export (Core · 30 s) ✅ hosted
- **Click:** chip **Export ANC 1 by district to Excel**
  ```
  Export ANC 1 Coverage by district for the last 12 months to Excel
  ```
- **Point at:** "148 rows" (13 districts × 12 months, less empty cells), the 5-row preview, and the **XLSX** and **CSV** buttons.
- **Do:** click **XLSX** and open it. Show the three sheets, **Data**, **Metadata** and **Sources**, so the file carries its own provenance.
- **Expected:** 148/148 rows matched.

#### Step 20. Resilience and "any DHIS2" (Core · 1 min, talk only, no new questions)
- **Point at:** the status pill again. If you like, open the second tab at `/api/dhis2/status` to show the three instances and their versions.
- **Talking points:**
  - **Multi-instance failover:**
    - The app checks a list of DHIS2 servers at startup and every 60 s, and uses the first that answers.
    - If the primary drops mid-request, it retries on the next one. The pill turns **Fallback** and the answer carries a badge.
    - If every server is down, the five suggested questions still answer from a **cached snapshot**, clearly labelled "Cached data as of 21 Oct 2026".
  - **Model fallback:** if Gemini is overloaded or out of quota, it retries, then switches to a backup model (Flash-Lite) by itself. Some of today's answers may already have come from the backup. You only notice it as a slower answer.
  - **Any DHIS2:** it reads org-unit levels, level names, geometry, indicator units and metadata from the connected instance at runtime. Live-tested on DHIS2 **2.40, 2.41 and 2.42**.
- Don't try to trigger a failover live. Describe it.

---

## 4. Handling questions from the audience

Keep answers short, plain and honest. If you don't know, say "I'll confirm that and come back to you."

**"Is our data safe? Who can see it?"**
- The hosted demo sits behind an access code, and every data request needs a signed session token.
- Towards DHIS2 the app only sends **read** (GET) requests. It cannot change or delete data.
  - We tested a prompt that told it to delete a data element and reveal its keys. It ignored the instructions and only answered the legitimate part of the question.
- What reaches the AI model: the question, metadata names, and the **aggregated** figures returned by DHIS2 Analytics, which it needs to write the summary. The tested flows use aggregate analytics, so no patient-level records are sent.
- In standalone mode it uses one DHIS2 account. On their own instance that should be a **read-only** user, so it only sees what that user can see.
- The code also supports signing in with a user's own DHIS2 token. This demo did not exercise that, so offer it as part of a pilot, not as a tested feature.

**"Which DHIS2 versions does it support?"**
- Tested live on **2.40.12, 2.41.10 and 2.42.7**.
- The map code also handles the older geometry API formats (pre-2.32), but only in automated tests. **2.38 and 2.39 have not been tested against a live server.** "We'd confirm on your version in week one of a pilot."

**"Which AI model does it use? What does it cost?"**
- Google **Gemini Flash**, with Flash-Lite as automatic backup. It is called through LiteLLM, so the provider can be changed by configuration. During testing it also ran on an open-weights model (gpt-oss-20b).
- The demo runs on Gemini's free tier and a free Render instance.
- **Per-question cost on a paid tier has not been measured yet**, so don't quote a figure. "We'd measure it during the pilot on your real question volume."

**"What if the internet or DHIS2 goes down?"**
- If one DHIS2 server fails, it switches to the next configured server by itself.
- If all are down, the five suggested questions answer from a cached snapshot, clearly labelled with its date.
- **It is not a full offline mode.** Other questions need a live DHIS2 and the model.

**"Can we run it on our own DHIS2?"**
- Yes, that's the pilot. It runs as one Docker container. You point it at your DHIS2 URL with a read-only account, and it syncs your metadata at startup (about 25 s on the demo database).
- Nothing in the map or resolution code is specific to Sierra Leone.
- In week one we'd agree a list of your real questions and check every answer against your Analytics API, the same way we checked today's figures.

**"How accurate is it?"**
- On 21 Oct 2026 each answer was checked by script against the DHIS2 Analytics API.
- On the hosted app: 23/24 on the original question set and 22/27 on a harder stress set. The remaining failures were then fixed and passed on the hosted app.
- Every answer shows its "Data used" line, so anyone can check it.

**"Why is ANC 1 coverage over 100%?"**
- The denominator is DHIS2's population estimate of pregnant women. Show Step 16: 253,269 / 243,951 = 103.82%. The tool reports DHIS2's own calculation.

**"Can it use web sources / WHO guidance?"**
- The code has optional web enrichment, but it is **not configured on this demo**. Don't demonstrate or promise it.

---

## 5. Recovery

| What happens | What to do |
|---|---|
| **Page won't load / very slow first load** | Cold start on the free instance (about 1 min plus the metadata sync). Say "it's on a free server that sleeps; it's waking up". Open `/health` in the backup tab. Once it returns `"status":"ok"`, reload the app and sign in again. |
| **Spinner for 30–40 s** | Normal when the backup model is answering. Keep talking: use the talking point of the current step, or explain the model fallback (Step 20). Wait up to about 60 s before you assume a problem. |
| **Error mentioning quota / "exceeded your current quota" / 429** | The app already retries on the backup model. Try the same question **once** more. If it fails again, switch to the chips, which are also in the offline snapshot, and use the backup files (PDF/DOCX/XLSX/PNG) from rehearsal. Quota resets around 08:00 WAT. |
| **"No data" / zero rows** | Check the "Data used" line: is it the place and period you meant? Re-ask with an explicit year and place, e.g. "… in Bo in 2025". Or move on to a backup question below. Don't argue with the screen; say "this period has no data in the demo database" if that's what the line shows. |
| **Map falls back to a chart with "I couldn't draw a map: …"** | That's the honest fallback (e.g. no boundaries for those units, or the app is in cached mode, where there is no geometry). Say so and show the chart. Then re-run Step 17's exact question, which was verified on the hosted app. |
| **Pill shows Fallback** | Fine. The answers come from another DHIS2 version of the same demo database. **Figures can differ slightly from this script.** Use it as a live failover moment. |
| **Pill shows Cached** | Only the five chips will answer, labelled "Cached data as of 21 Oct 2026". Run the chips and explain the failover. Skip free-typed questions. |
| **Pill shows Offline** | DHIS2 and the snapshot are both unavailable. Use the backup files and the screenshots. |
| **Wrong item picked** | Point at the "Data used" line: "this is why it shows its working". Re-ask using the exact DHIS2 name, e.g. "ANC 1 Coverage" instead of "antenatal coverage". |
| **Session expires / sent back to the sign-in screen** | Enter the access code you were given again. |

### Backup questions known to work (all verified on the hosted app)
```
Show me ANC 1 Coverage in Sierra Leone for the last 12 months
```
```
Show Penta 3 coverage <1y for each district in Sierra Leone for 2025
```
```
How many ANC 1st visits were recorded in Sierra Leone in 2025?
```
→ 253,269
```
Compare Measles doses given in Bo in 2024 versus 2025
```
→ 7,344 → 6,849
```
What was the Penta 1 to 3 dropout rate in Kenema district in 2025?
```
→ 7.75%
```
Which 3 districts had the lowest ANC 1 coverage in 2025?
```
→ Kono 53.81, Koinadugu 68.31, Kailahun 83.22
```
Compare BCG and measles coverage under 1 in Kailahun in 2025
```
→ BCG 28.30, Measles 23.23
```
Penta 1 to Penta 3 dropout rate by district for 2025
```
→ 13 districts, Kambia highest at 33.09, Kenema lowest at 7.75

---

## 6. Avoid on stage

These are known weak spots or things that haven't been verified. Don't type them live, and don't promise them.

1. **Answers with no AI model (mock / deterministic fallback).** Without the LLM:
   - "OPV3 coverage in Port Loko this financial year" picks the wrong item (OPV Stock PHU) (s9);
   - the misspelt "Measels coverage in Bombli district for 2025" returns "not available" (s16).

   Both work on the LLM path: the misspelt question gave Bombali Measles <1y = 18.96, though it took 37 s. Avoid misspellings and "financial year" on stage. Even on the LLM path, "this financial year" for OPV3 in Port Loko returns no data.
2. **Facility point maps.** On the demo database only **54 of 125** Bo facilities have coordinates, so a facility map looks sparse (the missing ones are listed honestly). A chiefdom map was verified locally only, and the Penta 3 map was verified locally but not on the hosted app. **On stage, only use the district ANC 1 map in Step 17.**
3. **National facility-level maps.** Thousands of points: not paginated, not tested.
4. **Slides mode / PPTX.** It is in the mode menu, but it was not covered by any QA run.
5. **Editing the report, then downloading it.** Browser edits are not carried into the PDF/DOCX.
6. **"Malaria confirmed" wording.** This failed in the first hosted audit, picking an item with zero rows. Later builds answer it via IDSR Malaria, but it's still a known trap. Use "IDSR Malaria".
7. **"Top N" questions.** An earlier build showed all 13 districts ranked instead of the top 5. The values were right but the list was too long. The "3 lowest" backup question above is fine. Avoid making a point of the exact count.
8. **Raw UID in the chart title for reporting-rate charts** (e.g. `BfMAe6Itzgt.REPORTING_RATE`). This was seen in the first audit and it is cosmetic. If it shows, say "that's the DHIS2 ID; the label is in the Data used line."
9. **Questions with no period or place**, other than the deliberate "Show me coverage" in Step 14. For example, "Give me malaria deaths across all districts" picks a different but defensible item.
10. **Follow-up questions that depend on the previous answer** ("now show that by chiefdom"). Not QA'd.
11. **Web enrichment or external sources.** Not configured on the demo.
12. **Triggering a failover live,** or claiming offline mode beyond the five chips.
13. **Claims about non-Sierra-Leone instances or DHIS2 2.38/2.39.** Covered only by automated tests, not live servers.
14. **Prompt-injection on stage.** It passed in testing, but the result is slow (about 29 s) and the screen shows little. Mention it in Q&A instead.
15. **Running the full script more than once on demo day.** It burns the Gemini quota.
16. **Calling it "in production" or "deployed for clients".** It's a working build on public demo data.

---

## 7. Closing and call to action

> "What you've seen: plain-English questions answered from live DHIS2 at every level from country to facility. Charts, maps, reports and Excel files, each showing the exact DHIS2 item and period behind it. It asks when a question is vague, and it says 'not available' instead of guessing.
>
> The next step is a short pilot on **your** DHIS2 instance:
> 1. We connect it to your instance with a read-only account.
> 2. Together we write down the 20–30 questions your managers actually ask.
> 3. We check every answer against your own Analytics API and share the results, including anything it gets wrong.
> 4. You decide whether it's useful, based on your data and not the demo database.
>
> Who on your team would be the right person to pick the questions with us?"

Hand over: one-page pilot outline and contact details. Offer to send the PDF report and Excel file from today's demo.

---

## Appendix: verified figures at a glance

All figures from DHIS2 2.41.10 (play demo database), checked against `/api/analytics` on 21 Oct 2026. "Rolling" means a relative period ("last 12 months" or "this year") that can move.

| Step | Question (short) | Expected | Checked on |
|---|---|---|---|
| 2 | ANC 1 Coverage, SL, last 12 months | 12 values; peak May 2026 139.39; low Dec 2025 79.37 (rolling) | hosted |
| 3 | Penta 3 <1y by district, 2025 | 13 districts; Western Area 66.49 high, Kono 10.88 low | hosted |
| 4 | IDSR Malaria trend, 12 months | 11 values (Apr 2026 empty); peak Jun 2026 1,974 (rolling) | hosted |
| 5 | ANC 1st visits, SL, 2025 | 253,269 | hosted |
| 6 | Fully immunised children by district, this year | data element; Western Area 15,751 high (rolling) | hosted |
| 7 | BCG doses by chiefdom, Bo, 2025 | 15 chiefdoms; Badjia 2,116 high, Selenga 96 low | hosted |
| 8 | ANC 1st visits, Ngelehun CHC, 2025 | 913 | hosted |
| 9 | Monthly Penta 3, Bo, 2025 | 12 months; Jan 20.65, Oct 63.70 peak | hosted |
| 10 | BCG by age, Bo, 2025 | <1y 7,941; >1y 1,024 | hosted |
| 10 alt | Measles fixed vs outreach, SL, 2025 | Fixed 47,151; Outreach 18,265 | hosted |
| 11 | Child Health on-time reporting, Bo, 2025 | 80.69% | hosted |
| 11 alt | Child Health reporting rate by district, last quarter | Bombali 98.61 high, Kono 72.16 low (rolling) | hosted |
| 12 | Child Programme BCG doses (program indicator), 2025 | 5,503 | hosted |
| 13 | Measles doses, Bo, 2024 vs 2025 | 7,344 → 6,849 (−6.7%) | hosted |
| 14 | "Show me coverage" | clarifying question with a numbered list | hosted |
| 15 | COVID-19 booster doses, Bo | "isn't available … haven't substituted" | hosted |
| 16 | ANC 1 numerator/denominator, SL, 2025 | 103.82 = 253,269 / 243,951 | hosted |
| 17 | Map: ANC 1 by district, 2025 | 13/13; Bo 146.73 high, Kono 53.81 low | hosted |
| 18 | Programme review report, ANC 1 | 12 values; mean 102.41, +26.6% (rolling); PDF + DOCX | hosted |
| 19 | Export ANC 1 by district to Excel | 148 rows; sheets Data / Metadata / Sources; CSV | hosted |
| backup | Penta 1–3 dropout, Kenema, 2025 | 7.75% | hosted |
| backup | 3 lowest ANC 1 districts, 2025 | Kono 53.81, Koinadugu 68.31, Kailahun 83.22 | hosted |
