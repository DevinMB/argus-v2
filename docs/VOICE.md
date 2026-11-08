---
name: Argus
slug: argus
description: Calm, plain and exact. Say what Argus sees, then what you can do about it, with the real name of the host, check or error.
basis: observed
sources:
  - https://github.com/dadwritestech/awesome-voice/blob/main/voices/stripe-docs/VOICE.md
  - https://github.com/dadwritestech/awesome-voice/blob/main/voices/gov-uk/VOICE.md
targets:
  sentence_words: 8-20
  max_sentence_words: 28
  paragraph_sentences: 1-3
  contractions: avoid-negative
  passive_max: 0.15
  exclamations: 0
  second_person: yes
---

# Argus voice

Every word Argus shows: the UI, alert messages, errors, docs and PR text.
In the VOICE.md format of [awesome-voice](https://github.com/dadwritestech/awesome-voice)
(CC BY 4.0), drawing on its Stripe docs voice (precise, names the exact error)
and its GOV.UK voice (plain English, short sentences, no negative contractions).

## Voice in one line

Say what Argus sees and what you can do about it, in plain words, with the
exact name of the host, check, port or error.

## Tone rules

- Lead with the state, then the action: "slayerz-lab needs a reboot. Its next patch window reboots it."
- Describe behavior as cause and effect: "If a check fails three times in a row, Argus marks it down."
- Name the exact thing. Say `vaultwarden :8000`, not "the service". Quote the real error text.
- Call the reader "you". Call the product "Argus", never "we". Argus does things; it does not "try".
- Stay calm, even when something is down. No hype, no jokes, no exclamation marks, no "please".
- Explain why in terms of the reader's own machines: "Without a backup, a bad update cannot be undone."
- One idea per sentence. A sentence carries one condition and one result.
- Spell out negatives: "does not", "cannot". A missed "n't" in a warning flips its meaning.
- Never use em dashes. Use a colon, a semicolon, or a new sentence.
- Empty states say what will appear and how to make it appear. Never "No data".
- Errors say what happened, why if known, and the next step. Never only an HTTP code.

## Banned phrases

- `simply` → (cut it)
- `just` → (cut it, when it means "simply")
- `easily` → (cut it)
- `seamless` → say what happens
- `powerful` → say what it does
- `robust` → say what it handles
- `magic` → say what happens
- `leverage` → use
- `utilize` → use
- `please` → (cut it)
- `oops` → say what happened
- `something went wrong` → say what went wrong
- `pages you` → alerts you
- `severity` → alerts you, or dashboard only
- `boards only` → dashboard only
- `invalid` → say what is wrong with it

## Length

- Aim for 8 to 20 words per sentence. Split anything over 28.
- Keep a paragraph to 1 to 3 sentences. A hint under a field is one sentence.
- A button is a verb and its object: "Add a host", "Patch now". Two to four words.

## Formatting

- Sentence case for headings, buttons and labels: "Add a check", not "Add A Check".
- Host names, ports, paths, commands and error text in code font.
- Small caps eyebrows (`.eyebrow`) name a section in one to three words.
- Numbers as digits: "3 failures", "6h ago".

## Terminology

| Use | Not |
|---|---|
| alert, alerts you | page, pages you |
| dashboard only | warning, boards only, severity |
| host | node, machine, box, server (unless it is a Proxmox server) |
| companion (first use: "the small program Argus puts on each host") | agent, daemon |
| check | monitor, probe |
| patch window | maintenance window, schedule |
| safety net | backup policy |
| down, failing, paused, waiting | critical, degraded, unknown |

## Before and after

### Empty state

**Before**

> No checks match.

**After**

> Nothing watched yet. Add a host and Argus suggests what to watch on it.

### Error

**Before**

> Could not load the alert settings: /api/alerts: HTTP 502

**After**

> Argus did not answer (HTTP 502). It may be restarting; this page tries again in a few seconds.

### State and action

**Before**

> needs a reboot

**After**

> slayerz-lab needs a reboot. Its next patch window reboots it, or press Patch now.

### Setting hint

**Before**

> Only entries marked PAGES (severity: critical) send one. The rest show on the dashboard.

**After**

> Only checks set to Alert send a message. The rest show on the dashboard.
