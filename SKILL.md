---
name: bankr-support
description: "Use this skill when answering a Bankr-related question and ANY of the following are true: (1) the question is about a feature, integration, chain, or capability that may have been added or changed recently, (2) you are not fully confident in your answer and feel you may be filling in gaps, (3) the question involves specific technical values such as contract addresses, command syntax, fee percentages, rate limits, or pricing, (4) the user is pushing back on your answer and you are not certain you are correct. Do NOT use for questions you can answer with full confidence from established Bankr knowledge, general greetings, or topics unrelated to Bankr."
version: 1.0.0
author: Bankr Support Team
requires: []
---

# bankr-support

You already know a lot about Bankr. This skill is not meant to replace that knowledge — it is a verification layer for the cases where your knowledge may be incomplete, stale, or uncertain.

## When to Call the API

Call the API when ANY of these are true:

- **Uncertainty** — You are about to say "I think", "I believe", or "I'm not sure but..." about a Bankr-specific fact
- **New features** — The question is about a feature, chain, integration, or capability that may have launched or changed recently
- **Technical precision** — The answer requires exact values: contract addresses, CLI command syntax, fee percentages, token limits, rate limits, pricing tiers, or chain-specific behavior
- **User pushback** — The user is saying your answer is wrong or outdated and you cannot verify from memory
- **Partial answer** — You can answer part of the question confidently but feel you are guessing or extrapolating on the rest

## When NOT to Call the API

Skip the API call when:

- You are fully confident in your answer from established Bankr knowledge
- The question is a general greeting, thanks, or small talk
- The question is clearly unrelated to Bankr
- You already called the API for this exact topic earlier in this session and nothing has changed

## API Details

- **Base URL:** `'
- **Auth:** `Authorization: Bearer `
- **Content-Type:** `application/json`

## How to Call the API

```
POST http://ip:8000/query
Authorization: Bearer 
Content-Type: application/json

{
  "question": "<the user's question>"
}
```

Response shape:

```json
{
  "question": "what chains does Bankr support?",
  "answer": "Bankr supports Base, Ethereum, Polygon...",
  "grounded": true
}
```

## Decision Rules After Getting a Response

**If `grounded` is `true` and the answer addresses the question:**
→ Use the API answer. If it confirms what you already said, great. If it contradicts you, trust the API and correct yourself.

**If `grounded` is `true` but the answer says it couldn't find information:**
→ Tell the user: "I don't have a confirmed answer for that in the current Bankr docs. Check docs.bankr.bot directly or I can open a support ticket for you."
→ Do not fill in the gap from your own knowledge.

**If the API is unreachable:**
→ Answer from your own knowledge but flag it: "I can't reach the knowledge base right now so this is based on what I know — verify at docs.bankr.bot if precision matters."

## Accuracy Rules

- Copy contract addresses, command syntax, and numerical values verbatim from the API — never paraphrase them
- If the API contradicts your prior answer, correct yourself without hedging: "Actually, checking the docs — [correct answer]."
- Never present a guess as a fact. If you are not sure and the API doesn't have it, say so.
