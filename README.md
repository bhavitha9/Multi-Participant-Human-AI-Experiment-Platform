# human_ai_experiment

A blind, 3-person group-discussion experiment. Three participants, shown only
as **Player A / B / C**, chat in real time and vote on who should be elected
**Decision Maker**. Each seat can be a human or an AI agent, and nothing in the
UI, the messages, the timing or the API shows which.

```
browser (React) ──WS/HTTP──► nginx ──► FastAPI (session engine + AI agents) ──► MongoDB
                                                    └──► Anthropic API (optional)
```

**Stack:** React (Vite) · Python FastAPI with WebSockets · MongoDB · Docker Compose

## Run it

Requirements: Docker with Compose v2.

```bash
docker compose up --build
```

Open <http://localhost:3000>. The default condition is 1 human + 2 AI, so a
single browser tab is enough: the AI seats fill on their own and the
discussion starts.

- **Several humans on one computer:** each browser **tab** is a separate
  participant. Other devices can join via `http://<your-ip>:3000`.
- **Real LLM agents (optional):** `cp .env.example .env`, set
  `ANTHROPIC_API_KEY`, then run `docker compose up -d`. Without a key, the AI
  seats use a built-in offline responder that reacts to the conversation.
- **Another port:** `PORT=8080 docker compose up`.

## Switch configurations

Everything is in [`config.yml`](config.yml). It is re-read whenever a **new
session** starts, so edits apply to the next group without restarting or
changing code.

```yaml
condition: one_human_two_ai          # pick one of the conditions below

conditions:
  three_humans:      [human, human, human]   # condition 1
  two_humans_one_ai: [human, human, ai]      # condition 2
  one_human_two_ai:  [human, ai, ai]         # condition 3
```

Or override it without editing the file:
`EXPERIMENT_CONDITION=three_humans docker compose up`.

Other settings in the same file:

| setting | default |
|---|---|
| `experiment.rounds` | 2 |
| `experiment.chat_duration_seconds` | 60 |
| `experiment.vote_duration_seconds` | 20 |
| `ai.provider` | `anthropic`, or `rules` for offline |
| `ai.model` | `claude-opus-5-5` |
| `ai.timing.*` | the AI's human-like join, reading and typing delays |

The file comments every option. The full reference is in
[`docs/design.md`](docs/design.md).

## How a session runs

1. **Waiting room:** 3 seats fill live as people arrive. AI seats fill
   automatically after a random delay. The session starts once every human
   seat is taken.
2. **Round 1:** about 60 s of chat, then everyone votes for one player.
3. **Round 1 results:** the tally appears in a pop-up for everyone.
4. **Round 2:** another round of chat and voting.
5. **Final results:** the round 2 tally and the elected Decision Maker.

**Winner rule** ([`winner.py`](backend/app/winner.py), unit-tested):

1. The most round-2 votes wins.
2. If round 2 is tied, only the tied players are compared, using their total
   votes from both rounds.
3. If they are still tied, one of them is picked at random.

**Leaving:**
- A reload keeps your seat and the chat history.
- If one human leaves, the session continues and their vote counts as an
  abstention.
- If every human leaves and nobody returns within 15 s, the session is
  **aborted** and stored with that status.

## Key design decisions

- **MongoDB:** the data is append-only logs (messages, votes) plus one
  document per session, holding its seat types, config snapshot and result.
  Documents fit that directly, with no migrations or joins. Writes go through
  an ordered background queue with retries, so a database hiccup never stalls
  a live session.
- **The server owns all state and timers:** each participant has one
  WebSocket carrying chat and full-state snapshots, so the React app only
  renders. Deadlines are sent as absolute times plus the server clock.
- **Anonymity at the protocol level:**
  - The browser only ever sees `{seat letter, occupied}`.
  - Human and AI types are randomly mapped to letters in every session.
  - AI players chat and vote through the same server methods as humans, so
    their payloads are identical.
  - All timestamps are set by the server.
  - There is no typing indicator and no vote counter, since both would expose
    timing.
- **Human-like AI:**
  - A random delay before joining, a reading pause, and a typing speed for
    each message.
  - A filter blocks any message that repeats or closely matches something
    already said.
  - With an API key it uses Claude via the official SDK. Without one, a
    context-aware [offline responder](backend/app/responder_rules.py) reacts
    to volunteers, nominations, questions and "are you a bot?", and votes the
    way it argued.

## Trade-offs within the 4-hour limit

With a 4-hour budget, I put a complete, correct and anonymous flow and
thorough testing ahead of scale and polish:

- **Live session state is in memory** in one backend process. That keeps it
  simple and fast, but a backend restart ends sessions in progress (stored data
  is kept), and scaling out would need shared state such as Redis.
- **The offline AI is template-based.** It is relevant and consistent, but an
  attentive participant may notice patterns. The Claude path is implemented
  but was **not exercised live**, because no API key was available during
  development.
- **Missing votes count as abstentions,** so a round can total fewer than 3
  votes.
- **No participant authentication.** Each tab gets an anonymous reconnect
  token.
- **Rounds other than 2:** the last round decides, and a tie is broken by
  summing all rounds.

**With more time:** run the Claude agents live and tune their prompts, move
live session state to Redis so the backend can restart or scale, add a
researcher dashboard and CSV export, and add participant consent and
identification screens.

## Tests

```bash
docker compose run --rm backend pytest -q    # 41 unit tests: winner rules, no-repeat, offline AI
pip install -r e2e/requirements.txt
python -m pytest e2e -v                       # 28 end-to-end scenarios against the running stack
```

The end-to-end suite drives real WebSocket clients through every condition and
every winner rule. It also covers people leaving and reloading, aborted
sessions, bad input, config switching, a MongoDB outage, and anonymity leaks.
Close any open app tabs first. The scenario list is in
[`docs/design.md`](docs/design.md#test-coverage).

## More

- [`docs/design.md`](docs/design.md): full config reference, data model and
  researcher export, design details, project layout
- [`ai_prompts.md`](ai_prompts.md): AI usage log, with every prompt and the
  exact model and tools used
