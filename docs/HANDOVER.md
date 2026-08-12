# Handover walkthrough — script + material (SOW §4.7)

One recorded session covers everything Aliyah does day-to-day. This file is the script
for that recording and doubles as the written onboarding material she asked for
(doc comment, 30 Jul: "written instructions that I can add to QwintiQ's Notion").

## What you (Aliyah) own

One page: **https://vault.qwintiq.com/admin** — your control panel. It does two things,
on purpose: manage who's in, and switch their access on/off. Everything else (the skills,
the frameworks, the sign-in wall) runs itself.

## Adding a consultant (≈20 seconds)
1. Open the control panel and sign in.
2. Type their name + email → **Add consultant**.
3. Send them the one-time temporary password the yellow banner shows (it appears once).
4. Send them the setup line for Claude Code:
   `claude mcp add --transport http qwintiq https://vault.qwintiq.com/mcp`
   First time they use a Qwintiq skill, a Qwintiq sign-in page opens — they enter their
   email + the temporary password. That's the whole setup; nothing else to install.

## Removing someone / pausing access (≈5 seconds)
- **Revoke key** — their access dies instantly, even if they're mid-session. The row
  stays so you can **Assign key** again later (fresh access, same account).
- **Remove** — takes them out entirely. Re-adding later gives a fresh password.

There is nothing to clean up on their computer: the skills were never files on it. What
they keep is a connector that no longer answers them.

## What consultants experience
They work in Claude Code exactly as before: "write Qwintiq copy", "size this market",
"write icebreakers for these people", "run today's partner signals". Claude calls the
vault, the vault returns the finished work. Counts are cheap and free-flowing; anything
that spends real credits stops and asks them to type the confirmation sentence with the
exact number, same as always.

## Recording checklist (for the live walkthrough)
- [ ] Sign in to the panel
- [ ] Add a demo consultant, show the one-time password banner
- [ ] Show the consultant side: sign-in screen → ask for copy → finished sequences back
- [ ] Revoke the key mid-session, show the next call being refused
- [ ] Assign the key again, show recovery
- [ ] Remove the consultant
- [ ] Where to go if something breaks: days 1–14 covered under the build warranty (SOW §10)
