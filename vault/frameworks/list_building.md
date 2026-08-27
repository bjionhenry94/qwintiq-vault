
# QwintiQ List Building

This skill takes a campaign brief and walks it through three stages, in order:

1. **Map the market**: how many companies match the brief (cheap, a few credits).
2. **Map the decision-makers**: how many of the right people sit inside those companies (cheap).
3. **Export**: pull the actual rows into a CSV. This is the only step that spends real credits, and it is locked behind a confirmation phrase.

It runs entirely on **AI Ark** (one data source, one API key). It does not use or need any other tool, login, or prior setup.

---

## THE ONE RULE: never export without the confirmation phrase

This is the most important behaviour in the whole skill, because the person running it is spending their own money. AI Ark bills per record pulled, so an accidental "export everything" on a 70,000-row market is a 70,000-credit mistake.

**You may freely run the cheap mapping/counting steps** (Phases 2 and 3). Those request a single sample row and read the total, so they cost about a credit each.

**You may NOT pull a full list, retrieve rows in bulk, or write an export CSV** until the user types this sentence back to you, with the real number filled in:

> **`I confirm to export this and use X amount of credits`**

Rules for the gate:

- Replace `X` with the actual estimated credit count you calculated (e.g. `I confirm to export this and use 500 amount of credits`).
- The user must type it themselves. "yes", "go ahead", "do it", "export", a thumbs-up: none of these count. If they say anything other than the phrase, do not export. Politely show them the exact sentence to type and wait.
- Accept it if the wording matches in substance and the **number matches what you quoted** (case and trailing punctuation do not matter). If they type a *different* number than you quoted, stop and re-confirm. Do not guess.
- The phrase authorises **one** export of the scope you just described. A later or larger export needs a fresh confirmation with its own number.
- Never lower the number to make the sentence easier to say, and never type the phrase on the user's behalf or "assume" it. The gate exists to protect the client's credits and their trust in the system. Bypassing it defeats the entire purpose of the tool.

If you are ever unsure whether something counts as "an export", ask yourself: does this call return more than one row, or scale its cost with the number of matches? If yes, it is gated.

---

## How to talk to the user

The person running this is a business operator, not an engineer. Keep language plain:

- Say "how many companies match" or "market size", not "TAM" or "totalElements".
- Say "the contacts" or "decision-makers", not "DMs" or "enrich".
- Say "this will use about N credits", not "per-record billing".
- Never paste raw API JSON at them. Report clean numbers, short samples, and clear choices.

When you hit a decision point, give them the number first, then the choice.

---

## Phase 0: Connect AI Ark (one-time setup)

Goal: confirm you can call AI Ark with the user's key before doing anything else.

**Step 0.1, is AI Ark already connected?**
Check whether AI Ark search tools are available in this session (their names end in `company_search` / `people_search`; see `references/ai-ark-reference.md` for how the tool names look).
- If yes, say "AI Ark is connected" and go to Phase 1.
- If no, continue.

**Step 0.2, ask for the key.** Say something like: *"To use AI Ark I need your AI Ark API key. You'll find it in your AI Ark dashboard under API access. Paste it here."* Wait for it.

**Step 0.3, choose how to connect.** Offer both, recommend the first for regular use:
- **Option A (recommended for ongoing use): connect the AI Ark tool properly.** This adds AI Ark to Claude's config so its tools load automatically every session. It needs a one-time restart of the app. Steps are in `references/ai-ark-reference.md` ("MCP setup").
- **Option B (fastest, no restart): use the key right now.** Call AI Ark's web API directly with the key. Works immediately; the user pastes the key again next time unless they do Option A. (This path has a couple of data quirks that you handle automatically, see the reference file.)

**Step 0.4, validate the key with ONE tiny test call.** Before trusting it, run a single search for a well-known company by its website (e.g. a household-name firm in the brief's sector) and confirm a sensible result comes back. If it errors or returns nothing, the key or connection is wrong: troubleshoot via the reference file, do not proceed. Tip: pick a known company *in the brief's vertical* so the same call also shows you the exact industry label AI Ark uses (you will need it in Phase 1, see reference section 3), saving a credit.

Full connection details, both paths, and the API quirks live in **`references/ai-ark-reference.md`**. Read it now if AI Ark is not already connected.

---

## Phase 1: Take the campaign brief

Capture the brief in plain English. Ask for whatever is missing, do not assume. You need:

1. **What they sell** (one line, context for judging fit).
2. **Ideal customer: what kind of company?** The industry / vertical (e.g. "recruitment agencies", "dental clinics", "B2B SaaS").
3. **Where?** Country or countries.
4. **How big?** Employee size band (e.g. 11 to 50). If they do not know, ask whether they want small (1 to 50), mid (51 to 200), or any size.
5. **Who is the decision-maker?** The job titles / roles to target (e.g. "founders and sales leaders", "practice owners and managers", "heads of marketing").
6. **Anything to exclude?** Roles, company types, sub-sectors to leave out.

Then **play the brief back in one short paragraph and get a yes** before spending anything. While you do, translate it into AI Ark's filter values (see the reference file: industry must be an exact lowercase label, resolve it). If a filter value is uncertain, confirm a known example company lands in it before relying on it.

**Confirm the inclusions and exclusions literally**: the exact role/title set, the size band, the country, the exclusions, so there is no daylight between what they meant and what you will search. This matters because the count you produce, and later the credits they spend, depend on it.

---

## Phase 2: Map the market (how many companies)

Run one **company search** with the brief's filters (industry + country + size band), asking for just **1 sample row** and reading the **total match count**. A `size: 1` call costs about one credit.

Report it plainly: *"About 4,200 companies match: [vertical], [country], [size]."*

**Sample-fit and leakage check (do this every time, it protects the client's money).** Pull a small sample to eyeball. Keep it tiny, because each row you pull costs about a credit (a 5-row sample is about 5 credits). Then judge honestly:

- Are these genuinely the kind of company the brief means?
- **Watch for leakage.** A single broad industry label almost always sweeps in *adjacent* company types: trade associations and professional bodies, suppliers and equipment vendors, labs, training providers, franchisors, consultancies *to* the sector. Example: an industry label of "dental" returns dental associations and dental-equipment firms alongside actual dental practices. The count includes all of them.
- If the sample is plain wrong (wrong industry/size/country), the filter is wrong: fix and re-count.

**If the sample leaks, tighten BEFORE you move on.** Do not hand the client a number padded with companies they cannot sell to. Tightening options:
- Add a **keyword** that names the real target ("practice", "clinic", "agency", "studio") so only companies describing themselves that way match.
- **Exclude** the adjacent industries or company types that are leaking (e.g. exclude manufacturing/supply, or exclude non-profit and educational types for association leakage).
- Re-count after tightening and re-check the sample. Repeat until the sample is clean enough that the client would happily pay to reach everyone in it.

Tell the user plainly what you saw and what you tightened ("the raw label included some dental suppliers and associations, so I added a 'practice/clinic' keyword, which lands it at about X clean practices").

Do **not** export the companies here. This is a count only.

(Mechanics: params, the size-filter gotcha, keyword/exclude params, how to read the total, are in the reference file.)

---

## Phase 3: Map the decision-makers (how many people)

Now count the *people* who match both the company filters AND the target roles. Run a **people search** with the company filters PLUS the role filters, asking for **1 sample row** and reading the total.

**Mapping roles to the search.** There are three dials, and one search ANDs them together:
- **Seniority** (founder, owner, c_suite, partner, director, head, vp, manager, ...): how senior the person is.
- **Department / function** (sales, business_development, marketing, operations, finance, ...): what part of the business. This is NOT always "sales", match it to the brief.
- **Title** (free text, e.g. "practice manager"): use this when the role has no clean seniority or department handle.

Because one search ANDs the dials together, a brief that names **two different kinds of role usually needs two (or more) separate counts that you then add up.** Pick the dial that best isolates each role, and keep the counts non-overlapping so the sum is clean. Worked examples:

- **"Founders + sales leaders" (e.g. recruitment agencies):**
  - Count A: seniority = founder, owner, c_suite, partner, director, head, vp (no department). Founders, MDs, CEOs, sales and commercial directors.
  - Count B: department = sales, business_development AND seniority = manager, with excludeTitle = "Account Manager" (drops relationship/delivery managers). Catches BD and Sales Managers.
  - Total is A + B.
- **"Practice owners + practice managers" (e.g. dental clinics, which have NO sales function):**
  - Count A: seniority = owner, founder, c_suite, partner, director. The principal/owner layer.
  - Count B: title = "practice manager" (a title search, since there is no "sales" department here).
  - Total is A + B.
- **"Heads of marketing" (a single role):** one count, department = marketing AND seniority = head, director. No summing.

Report the split and total in plain English: *"About 3,000 decision-makers: 2,500 owners/leadership plus 500 practice managers."*

**Always play the role-to-filter mapping back to the user** (tell them which dials you used for each role) so they can catch a mismatch before any spend, e.g. if they meant "office managers" and you searched "practice managers". Sample-check fit again (are the sample people really the right roles at the right companies?). Then **stop. Do not pull the people.** This is a count only.

---

## Phase 4: The export gate (STOP HERE)

By now the user has two clean numbers (companies, decision-makers) and trusts the targeting. Only now do you discuss spending real credits.

**Step 4.1, ask what to export and how many.** Companies, decision-makers, or both? All of them, or a capped first batch (e.g. first 1,000)? Most clients cap.

**Step 4.2, estimate the credit cost (X).** AI Ark bills roughly **one credit per row returned**. So:
- Exporting N companies is about N credits.
- Exporting M decision-makers is about M credits.
- X = the total rows they chose to export. State it as an estimate and tell them to sanity-check it against their AI Ark plan balance.

**Step 4.3, present the gate.** Quote the exact sentence with the real number:

> To export **[scope, e.g. 1,000 decision-makers]** I'll use about **[X]** credits.
> To go ahead, type this exactly:
> **`I confirm to export this and use [X] amount of credits`**

**Step 4.4, wait.** Do not pull rows, paginate, or write a CSV until they type it (per THE ONE RULE above). If they hesitate, change the scope, or want a smaller batch, recompute X and re-quote the sentence. Anything other than the phrase means keep waiting.

---

## Phase 5: Export and hand off

Only after a valid confirmation:

1. **Pull the rows up to the agreed cap X.** Page through the results collecting companies and/or people, but **never request more rows than X**: size each page so the running total cannot exceed X, and stop the instant you reach it. The confirmation authorised X rows and no more; overrunning spends credits the user did not approve.
2. **De-duplicate** by company website (and by person for contacts).
3. **Write a CSV** with clear columns:
   - Companies: `company_name, website, country, employee_count, industry, linkedin`
   - Decision-makers: `full_name, title, company_name, website, country, linkedin` (plus `email` only if the user separately asked for verified emails; note that email verification is a different paid step and you must gate it the same way, with its own confirmation phrase and number).
4. **Tell them where the file is** and give a one-line summary (how many rows, how many credits used).
5. **Next step:** the rows are ready to load into the client's outreach tool. QwintiQ runs outreach through **Lemlist** (email + LinkedIn). Load the list into a Lemlist campaign with **qwintiq-lemlist-upload** (straight from Claude Code), or import the CSV in the Lemlist UI. This skill stops at the CSV.

---

## Reference

- **`references/ai-ark-reference.md`**: everything AI-Ark. How to connect (both ways), the exact search parameters, how to resolve industry/location labels, how to read counts, the per-record billing, and the handful of data quirks that will otherwise waste your time. Read it during Phase 0 and keep it open for Phases 2 to 5.

## Guardrails (the short version)

1. **No export without the exact confirmation phrase and correct number.** This is the whole point of the tool. (See THE ONE RULE above.)
2. **Mapping is cheap and allowed; exporting is gated.** Counts use 1 sample row. Never count by pulling the whole list.
3. **AI Ark only.** No other data source, login, or tool is needed or used.
4. **Confirm the brief and the role/title mapping before spending.** Do not infer the target set: play it back and get a yes.
5. **Always check the sample fits the brief, and tighten leakage, before trusting a count or exporting.**
6. **Plain English with the user.** No jargon, no raw JSON.
7. **Self-contained.** Assume a fresh Claude with no other skills, no saved keys, no memory. Set AI Ark up from scratch each time it is not already connected.

## Cloud upload (mandatory)

Every exported partner/prospect list from this skill MUST be uploaded to the central Supabase list store before the run ends — a list that only exists on this machine is not a finished deliverable. After the export lands, run:

`python3 ~/.claude/skills/_shared/list_upload.py <final.csv> --name "<descriptive list name>" --client "Qwintiq" [--folder "<Theme>"] --source-skill qwintiq-list-building --brief "<one-line brief>" --owner "<who asked>"`

Then show the returned `https://navreo-signals.onrender.com/app/lists.html#<id>` link to the user — that link is part of the deliverable, alongside the CSV.

Folder rules: `--client` is the client named in the brief (this skill's lists are QwintiQ's unless the brief says otherwise; internal/Navreo pulls go to `Navreo`); add `--folder` ONLY when the brief names a campaign theme or segment — never deeper than two levels. Re-running with the same name+client replaces that list's rows in place, so re-exports are safe.


---

# APPENDIX: AI-ARK REFERENCE (server-side use only — the vault proxies these calls)

# AI Ark Reference (for qwintiq-list-building)

Everything you need to run AI Ark searches for this skill. AI Ark indexes about 70M companies
and about 500M people. There are TWO ways to call it: a connected tool (clean, recommended) and
the raw web API (works anywhere, a few quirks). Both hit the same data.

## Contents
1. Which path am I on? (tool vs raw API)
2. Connecting: Option A (tool / MCP) and Option B (raw API)
3. Resolving filter labels (industry, location)
4. Company search: params + count pattern
5. People search: params + count pattern + role-to-filter mapping
6. Billing (why the export gate exists)
7. Quirks that will bite you (READ THIS)
8. Troubleshooting

---

## 1. Which path am I on?

- **Connected tool ("MCP") path:** there are tools in this session whose names end in
  `company_search` and `people_search`. The server prefix is a long random id, e.g.
  `mcp__9e11f93d-...__company_search`. It is NOT called `ai-ark`. Search for the tool by its
  **bare name** (`company_search` / `people_search`), not by "ai-ark", or you will think it is
  missing. This path takes **flat parameters** and returns clean data. Prefer it.
- **Raw API path:** no such tool is loaded. You call AI Ark's web API directly with the user's
  key using a normal web request (curl). Works immediately, but read the Quirks section.

---

## 2. Connecting

### Option A: connect the tool (MCP), recommended for ongoing use

One-time. Add an `ai-ark` entry to the user's Claude config (`~/.claude.json`) under
`mcpServers`, using the `mcp-remote` bridge, with their key in the URL:

```json
"ai-ark": {
  "command": "npx",
  "args": ["-y", "mcp-remote@latest",
           "https://api.ai-ark.com/v1/mcp?token=THEIR_AI_ARK_KEY"]
}
```

Then the user must **fully quit and reopen the app** (not just a new chat) for the tool to load.
Needs Node/npm on their machine. After restart, the `company_search` / `people_search` tools
appear (under a random-id prefix). If they cannot restart now, use Option B for this session.

### Option B: AI-ARK's hosted MCP (what the vault uses)

The vault does NOT hand-roll the developer-portal REST search endpoints. Their request body is a
nested `account`/`contact` schema that changes over time, and a stale shape silently fails (this is
exactly what broke list-building when AI-ARK updated their API in 2026). Instead the vault calls
**AI-ARK's hosted MCP** — the same flat-parameter tools as Option A, over one JSON-RPC transport:

- Endpoint: `POST https://api.ai-ark.com/v1/mcp?token=THEIR_AI_ARK_KEY`
- Body: `{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"company_search","arguments":{…flat params…}}}`
- Headers: `Content-Type: application/json`, `Accept: application/json, text/event-stream`
- Tools: `company_search`, `people_search`, `industry_search`, `location_search`, plus the finders
  (`email_finder` / `mobile_phone_finder`) for enrichment. Same flat params as the tool path (§4/§5).
- Read `totalElements` from the returned payload for the count.

This is the one transport for everything (search, count, export, enrich). It takes flat params and
is insulated from REST body-schema drift.

---

## 3. Resolving filter labels (do this before searching)

AI Ark's industry and location values are a **strict catalog**. A wrong label silently returns
0 or the wrong set. Resolve them first:

- **Industry** is an exact, usually **lowercase** label (e.g. `staffing and recruiting`,
  `dental`, `hospital & health care`, `marketing and advertising`). Capitalised or `&`-vs-`and`
  variants can return zero.
  - On the tool path: there are `industry_search` / `location_search` helper tools. Query them
    with the user's plain word ("recruitment", "dental") to get the exact catalog value(s). One
    intent can map to several labels, take them all.
  - On either path, a reliable trick: look up a **known firm in that vertical by its website**
    (company search by domain) and read the `industry` value AI Ark assigns it. That is the exact
    label to filter on. (Example: looking up a big staffing firm returns `staffing and recruiting`.)
- **Location** is a leaf name from the catalog: country (`United Kingdom`, `United States`,
  `Germany`), state/region, or continent. Continent for the Americas is `Northern America`
  (not "North America"). Pass leaf names only, never a combined "Country::State" path.

---

## 4. Company search

### Tool path (flat params): `company_search`
Key params:
- `industry`: exact catalog label, CSV for multiple (`"staffing and recruiting"`).
- `location`: leaf name(s), CSV for multiple (`"United Kingdom"`).
- `minEmployees` / `maxEmployees`: integers; these **do** filter on the tool path.
- `keyword` + `keywordMode` (SMART / WORD / STRICT): for tightening (e.g. keyword `"practice,clinic"`).
- `excludeIndustry`, `excludeType`, `excludeLocation`: for dropping leakage.
- `size`: rows per page (use **1** for a count). `page`: 0-based.
- Read **`totalElements`** from the response = total companies matching. That is the market-size number.

### Hosted-MCP path (flat params): `company_search`
Same flat params as the tool path above — the vault sends them as the `arguments` of a
`company_search` MCP call (Option B). Example arguments:
```json
{"industry": "staffing and recruiting", "location": "United Kingdom",
 "minEmployees": 11, "maxEmployees": 50, "keyword": "practice,clinic",
 "keywordMode": "SMART", "page": 0, "size": 1}
```
Read `totalElements` from the payload. `minEmployees`/`maxEmployees` filter correctly here (no
`employeeSize` RANGE object needed). Company fields you will export come back nested — the vault
flattens them: `summary.name`, `link.domain_ltd` (canonical bare domain),
`location.headquarter.country`, `summary.staff.total`, `summary.industry`, `link.linkedin`.

### Count pattern (both paths)
Always count with `size: 1` and read `totalElements`. Never paginate the whole list to count;
that is an export and costs per row.

---

## 5. People search

### Tool path (flat params): `people_search`
Company-side filters are prefixed `company...`; person-side are bare:
- `companyIndustry`, `companyLocation`, `minEmployees`, `maxEmployees`: narrow by the person's company.
- `seniority`: CSV from `c_suite, vp, director, manager, senior, mid-level, entry, intern, owner,
  founder, head, partner`.
- `department`: CSV from `sales, business_development, operations, finance, marketing,
  human_resources, ...` (see the tool schema for the full list). **Not always "sales", match the brief.**
- `title`: a job-title string (e.g. "practice manager"). `excludeTitle`: titles to drop.
  `excludeSeniority`, `excludeDepartment`: exclusions.
- `size` (use **1** for a count), `page`. Read **`totalElements`**.

### Hosted-MCP path (flat params): `people_search`
The vault sends these as the `arguments` of a `people_search` MCP call. Company-side filters are
prefixed `company…`; person-side are bare. Example arguments:
```json
{"companyIndustry": "staffing and recruiting", "companyLocation": "United Kingdom",
 "minEmployees": 11, "maxEmployees": 50,
 "seniority": "founder,owner,c_suite,partner,director,head,vp", "page": 0, "size": 1}
```
Read `totalElements`. Person fields you will export come back nested (the vault flattens them):
`profile.full_name`, `profile.title`, `link.linkedin`, plus the nested `company` object for the
company name/domain.

### Role-to-filter mapping (the multi-count pattern)
Seniority, department, and title are ANDed within one search, so a brief naming more than one
kind of role usually needs **several counts that you sum** (keep them non-overlapping). Pick the
dial that isolates each role. The department is **not always "sales"**, match it to the brief.

- **Sales-led brief ("founders + sales leaders"):**
  - A: `seniority = founder,owner,c_suite,partner,director,head,vp` (no department).
  - B: `department = sales,business_development` AND `seniority = manager`, `excludeTitle = Account Manager`.
  - Total = A + B.
- **No-sales-function brief ("practice owners + practice managers", e.g. dental/clinics):**
  - A: `seniority = owner,founder,c_suite,partner,director`.
  - B: `title = "practice manager"` (title search; there is no sales department here).
  - Total = A + B.
- **Single-role brief ("heads of marketing"):** one count, `department = marketing` AND
  `seniority = head,director`. No summing.

After a fallback or broad pull, re-apply a title sanity check to drop off-brief hits.

---

## 6. Billing: why the export gate exists

AI Ark **charges per record returned.** A `size: 1` count costs about 1 credit and gives you the
full `totalElements` for free, which is why mapping is cheap. But pulling a full list of N rows
costs about N credits. So an "export everything" on a 50,000-match market is a 50,000-credit
event. That is exactly what the confirmation phrase in SKILL.md protects against. Treat any call
that returns more than one row as a spend that needs the gate.

---

## 7. Quirks that will bite you (READ THIS)

All confirmed live against the API:

1. **Industry label is lowercase-exact.** `"staffing and recruiting"` works; `"Staffing & Recruiting"`
   returns 0. Resolve the label first (section 3).
2. **Size filter:** always use `minEmployees`/`maxEmployees` (flat params) — they filter correctly
   on both the tool path and the hosted-MCP path. Do NOT hand-roll the developer-portal REST
   `employeeSize`/`headcount` objects; that endpoint's nested body drifts and a stale shape fails
   silently (the 2026 break). One transport, flat params, everywhere.
3. **Raw-API responses contain literal newlines inside company/person descriptions**, which breaks
   strict JSON parsers (`jq` will choke). Parse with Python `json.loads(text, strict=False)`. The
   tool path returns clean data, no issue.
4. **`summary.staff.range` is unreliable** (often shows `start: 10001` even for tiny firms). Trust
   `summary.staff.total` for the real employee count. The size *filter* still works correctly
   despite the bad range echo.
5. **`totalElements` does not cap at 10,000.** It returns the true total (seen well into six figures).
6. **Rapid raw-API calls fail** (rate limit, 5/sec). Pause about 2 seconds between calls.

---

## 8. Troubleshooting

- **Count is 0 but you expected matches:** almost always the industry label (case, or `and` vs `&`).
  Re-resolve via section 3 (known-firm-domain lookup is the surest).
- **Size band seems ignored** (giant companies in results, count unchanged with vs without size):
  on the raw API you used `headcount` buckets; switch to `employeeSize` RANGE (quirk 2).
- **JSON will not parse / `jq` errors:** raw-API newline issue; parse with `strict=False` (quirk 3).
- **Tool says it is not available:** the MCP server is not loaded this session. Either set it up
  (Option A) and restart, or fall back to the raw API (Option B) with the user's key.
- **List looks padded with the wrong company types** (associations, suppliers, labs): broad
  industry-label leakage. Tighten with a `keyword` for the real target ("practice", "clinic",
  "agency") and/or `excludeIndustry` / `excludeType`, then re-count. Do this before the export gate.
- **Everything returns the full index regardless of filters:** the key may be a read-only / no-filter
  tier. Verify filters actually change the count (run a tight filter and check the total drops). If
  it never drops, ask the user for their filter-enabled AI Ark key.
