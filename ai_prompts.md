# AI Usage Log

## Tools and model

| | |
|---|---|
| AI assistant / agent | **Claude Code**, in the Claude desktop app (Code tab), running as an autonomous coding agent |
| Model | **Claude Opus 5.5** (`claude-opus-5-5`), the only model used |
| Agent tools the assistant used | shell (Bash: Docker, pytest, git), file read/write/edit, the app's built-in browser (to click through the UI and read the console), a folder picker and multiple-choice questions to me |
| Model used *inside* the app | The AI participants call Claude through the Anthropic API (`ai.provider: anthropic`, default model `claude-opus-5-5`). No API key was configured during development, so the app ran on its offline `rules` responder |

Everything was done in a single Claude Code conversation (2026-11-15 to
2026-11-17). Below are **all of my prompts, verbatim and in order**, including
my answers to the assistant's multiple-choice questions. Each has a short note
on what the assistant did in response.

---

### Prompt 1 (2026-11-15): the task

```text
You are a senior full-stack developer. Build a web application called
"human_ai_experiment": a blind, 3-person group-discussion experiment. Put all
files inside a root folder named `human_ai_experiment`.

## Objective
Each session has 3 participants who discuss and vote on who should be elected
"Decision Maker" for a subsequent (imaginary) experiment. Participants must not
be able to tell whether the others are humans or AI agents.

## Tech stack (required)
- Frontend: React
- Backend: Python, FastAPI (real-time via WebSockets)
- Database: MongoDB or PostgreSQL (your choice; state which and why)
- Containerization: Docker. The whole app must start locally with a single
  command (`docker compose up`).

## Configuration
Use a `config.yml` file to control experiment parameters and participant types
(`human` or `ai`), so all three conditions can be tested without code changes:
1. 3 humans
2. 2 humans + 1 AI agent
3. 1 human + 2 AI agents

Also make these configurable: round duration (default ~60s), number of rounds
(2), and the LLM/provider settings for the AI agents.
Participants and anonymity
- Neutral identifiers only: Player A, Player B, Player C.
- Nothing in the UI, message format, timing, or API responses may reveal
  whether a participant is human or AI.

User flow
1. Waiting room
   - On opening the browser, a human lands in a waiting room showing 3 seats.
   - All seats start empty. Each joining player occupies a seat in real time;
     unfilled seats stay visibly empty.
   - AI seats are filled automatically. The experiment starts once all human
     slots in the config are filled.
2. Round 1: about 1 minute of real-time text chat about who should be Decision
   Maker, followed by a vote. Each participant votes for one of the 3 players.
3. Round 1 results: show the vote results in a pop-up for all participants.
4. Round 2: another ~1 minute of chat, then a second vote.
5. Final results: display the round 2 results and the elected Decision Maker to
   all participants.

Winner calculation (implement exactly)
1. Round 2 decides: the player with the most round-2 votes wins.
2. If round 2 is tied: among only the tied players, sum their votes from both
   rounds. The highest total wins.
3. If still tied: pick randomly among the tied players.


Deliverables
- Complete, runnable source code in `human_ai_experiment/`
- `docker-compose.yml` and Dockerfiles for frontend, backend, and database
- `config.yml` with commented examples for all 3 conditions
- Persistence of sessions, messages, votes, and results in the database
- `README.md` with setup, usage, config instructions, and the design decisions above
- Unit tests for the winner-calculation logic, covering all three tie-break cases
```

The assistant built the whole stack: FastAPI + WebSockets, React (Vite) behind
nginx, MongoDB, Docker Compose, `config.yml`, winner logic with unit tests, and
the README. It then ran all 3 conditions with scripted clients and clicked
through the UI in the browser. That testing found and fixed three bugs:

- a Docker single-file mount breaking when `config.yml` was edited
- a React crash caused by `scrollIntoView` returning a Promise
- AI votes arriving after the vote deadline

---

### Prompt 2 (2026-11-17)

```text
yes please move it nd test it i will crosscheck it again
```

The project was moved from a temporary folder to `~/Human-ai` (I picked the
folder in a folder picker). The assistant rebuilt it there and reran all
checks: unit tests, all 3 conditions, each winner rule live, the browser flow,
and the database contents.

### Prompt 3

```text
yes proceed with testing
```

### Prompt 4

```text
ai replies shoudn't repeated sentences then it would be easily understandable  lets add anthropic_api_key
```

The assistant added a fuzzy repeat check: an AI never posts anything identical
or very similar to a message already in the chat. It also created a
git-ignored `.env` for the API key and asked me to paste the key in myself
rather than in chat.

### Prompt 5

```text
okay then lest ingnore api key for now
```

### Prompt 6

```text
i feel ai conversation is not relevent to the human conversations
```

The assistant replaced the random canned replies with an offline,
context-aware responder (`backend/app/responder_rules.py`). It reacts to
volunteers, nominations, direct questions, "why", "are you a bot?" and the
previous round's results, and it votes in line with what it said. The
assistant iterated on it with scripted live conversations and fixed several
issues they showed: duplicate lines between the two AIs, questions that went
unanswered, and stale replies carried into round 2.

### Prompt 7

```text
Let's go through each possible scenario one by one and test them systematically 1 AI + 2 humans, 2 AIs + 1 human and so on We should also cover all the edge cases to verify whether each combination works as expected or if anything breaks i  want to test every possible scenario thoroughly
```

The assistant wrote an end-to-end scenario suite (`e2e/`) that drives the
running Docker stack over WebSockets. The first run found three real bugs,
which it fixed:

- stale empty waiting rooms were reused, so the seat letter never changed and
  config edits were ignored
- a malformed message dropped the participant's connection
- a MongoDB outage froze live sessions

### Prompt 8

```text
lets check now
```

### Prompt 9 (sent while the assistant was working)

```text
lets say we have 2 human and 1 ai if 2 humans left the chat in the middle then it will say aborted right is that happening
```

The assistant confirmed this wasn't happening: the session kept running with
only the AI. It added abort-on-abandonment with a grace period
(`abort_after_all_humans_left_seconds`), an "aborted" status in the database,
a "Session ended" screen, and end-to-end tests for this scenario.

### Prompt 10

```text
if everything is fine then create these for me 
dont count this prompt any where 

1. GitHub Repository (Due within 7 days): Share a link to a public GitHub repository containing:
   * Your source code, `config.yml`, and Docker setup.
   * A concise `README.md` explaining how to run the project, switch configurations, and any key design decisions or trade-offs made within the 4-hour limit.
   * AI Usage Log: A file or folder in the repository (e.g., `ai_prompts.md` or `ai_logs/`) containing all prompts from your conversations with your AI assistant(s), along with the exact model(s) and agent/tool(s) you used.
   * Send the link to the Git Repository in response to this email.
2. Online Interview (5-Minute Presentation + Demo):
   * Prepare a short slide deck to present your development process in 5 minutes (how you broke down the problem, prompted/guided your AI agent, resolved issues, and made trade-offs).
   * Be ready to run the app locally via Docker, demonstrate the flow, and discuss your codebase.i need to
i need to submit those help me do it
```

The assistant pointed out that the assignment asks for *all* prompts and
recommended keeping this one in the log. My answers to its questions:

- **"How should I handle the prompt asking me to prepare the submission?"**
  I chose: *Include it (Recommended)*.
- **"How should the public GitHub repository be created?"** I chose: *I'll
  create it on github.com*.

It then wrote this log, the README notes, the git repository and the
presentation deck.

### Prompt 11 (sent while looking at the slide deck)

```text
i want to include in slides only these other if relevnt keep it if not remove it
how you broke down the problem, prompted/guided your AI agent, resolved issues, and made trade-offs
```

The assistant cut the deck from 9 slides to 6: cover, breaking down the
problem, guiding the AI agent, resolving issues, trade-offs, and a closing
demo slide. It folded the test results into the issues slide and kept my own
edit to the cover title.

### Prompt 12

```text
dont push yet we will finalise evrything at last we will push nd now lets do this 
```

followed by this pasted text:

```text
1. GitHub Repository (Due within 7 days): Share a link to a public GitHub repository containing:
   * Your source code, `config.yml`, and Docker setup.
   * A concise `README.md` explaining how to run the project, switch configurations, and any key design decisions or trade-offs made within the 4-hour limit.
   * AI Usage Log: A file or folder in the repository (e.g., `ai_prompts.md` or `ai_logs/`) containing all prompts from your conversations with your AI assistant(s), along with the exact model(s) and agent/tool(s) you used.
   * Send the link to the Git Repository in response to this email.
```

The assistant made the README concise and moved the detail to
`docs/design.md`. It brought this log up to date and checked the repository
from a fresh clone, as a reviewer would, without pushing.

### Prompt 13

```text
give me the files now
```

The assistant packaged the repository as a zip of the committed files and
explained how to download the slide deck.
