# human_ai_experiment

A blind, 3-person group-discussion experiment. Three participants, shown only as
**Player A / B / C**, chat in real time and vote on who should be elected
**Decision Maker** for a later (imaginary) experiment. Each participant can be a
human or an AI agent, and nothing in the UI or the protocol shows which.

```
browser (React) ──HTTP/WS──► nginx (frontend container) ──► FastAPI (backend) ──► MongoDB
                                                               └─► Anthropic API (AI seats)
```

## Quick start

Requirements: Docker with Compose v2.

```bash
cp .env.example .env          # optional: add ANTHROPIC_API_KEY for real AI agents
docker compose up --build
```

Open <http://localhost:3000>. With the default condition (`one_human_two_ai`), a
single browser tab is enough: the two AI seats fill on their own after a few
seconds and the discussion starts.

- **No API key?** The backend logs a warning and uses the offline `rules`
  responder. It reads the chat and replies to it, so the full flow works
  without a key, but replies are template-based. See "AI agents" below.
- **Multiple humans on one machine:** each browser **tab** is its own participant
  (the reconnect token is kept in `sessionStorage`). Open 2 or 3 tabs, or use
  other devices on your network via `http://<your-ip>:3000`.
- **Change the port:** `PORT=8080 docker compose up`.

Run the tests:

```bash
# unit tests: winner rules, no-repeat, offline responder
docker compose run --rm backend pytest -q

# end-to-end scenario matrix against the running stack (~15 min)
pip install -r e2e/requirements.txt
python -m pytest e2e -v
```

Close any browser tabs that have the app open before running it, because they
reconnect automatically and would take seats. The end-to-end suite connects real WebSocket clients through nginx, the same
way browsers do. Between scenarios it switches `config.yml` to fast timings
and restores your original file at the end. It also checks what was stored in
MongoDB. It covers:

| area | scenarios |
|---|---|
| conditions | full 2-round session for 3 humans, 2 humans + 1 AI, 1 human + 2 AI: phase order, same result for everyone, every seat voted, AI didn't repeat itself, everything persisted |
| winner rules | round-2 majority, round-2 beats the round-1 leader, 3-way tie broken by totals, only tied players counted, still tied gives a random pick among them, all self-votes, nobody votes |
| waiting room | seats fill live, no start until every human is there, leaving frees the seat, AI seats fill on their own but not instantly, seat letters vary between sessions, a 4th person gets a separate session that runs independently |
| leaving / reloading | reload keeps seat, history and result; unknown token gives a new seat; a second tab replaces the first; one human leaves and it continues; everyone leaves and it is aborted; everyone leaves but someone returns in time and it continues |
| bad input | non-join first message, chat/vote in the wrong phase, malformed JSON, unknown types, empty/long/HTML messages, message flooding, invalid and changed votes |
| configuration | broken or unknown condition reported without crashing, config change applies to the next session, 1 round, 3 rounds, `EXPERIMENT_CONDITION` override, admin export token |
| robustness | MongoDB stopped mid-session: the session finishes on time and the data is saved once the database is back |
| anonymity | in every scenario, no payload mentions human/AI, and states, seats and messages carry only the allowed fields |

## User flow

1. **Waiting room.** There are 3 seats, all empty at first. Your seat fills when
   you arrive and other seats fill live as others join. AI seats fill
   automatically after a random delay. Once every seat is taken there is a short
   countdown.
2. **Round 1.** About 60 s of real-time chat, then a vote. Everyone votes for
   one of the 3 players, and voting for yourself is allowed. The vote phase ends
   once all 3 have voted, or when the vote timer runs out (a missing vote counts
   as an abstention).
3. **Round 1 results.** A pop-up shows the vote tally to everyone.
4. **Round 2.** Another chat, then a second vote.
5. **Final results.** Everyone sees the round 2 tally, the elected Decision
   Maker, and which rule decided it.

Leaving and reloading:

- **Reloading the page** keeps your seat and the chat history.
- **Closing the tab in the waiting room** frees your seat for someone else. If
  the waiting room ends up with no humans in it, it is discarded. The next
  person starts a fresh session with the current `config.yml` and a new random
  seat assignment.
- **One human leaves mid-session** (others remain): the session continues. The
  missing person's votes count as abstentions.
- **Every human leaves mid-session:** if nobody returns within
  `abort_after_all_humans_left_seconds` (15 s by default), the session is
  **aborted**. The round timers and the AI players stop, no Decision Maker is
  elected, and the session is stored with `status: "aborted"` and an
  `abort_reason`. Anyone who reopens the page sees "Session ended". Returning
  within the grace period (a reload or a network blip) lets the session
  continue normally.

## Configuration (`config.yml`)

`config.yml` is **re-read every time a new session is created**, so edits apply
to the next group. You don't need to restart anything or change code. A session
already in progress keeps the settings it started with.

### Choosing the condition

```yaml
condition: one_human_two_ai        # which entry below is active

conditions:
  three_humans:       [human, human, human]   # Condition 1
  two_humans_one_ai:  [human, human, ai]      # Condition 2
  one_human_two_ai:   [human, ai, ai]         # Condition 3
```

You can also override it without editing the file:

```bash
EXPERIMENT_CONDITION=three_humans docker compose up
```

(or set `EXPERIMENT_CONDITION` in `.env`). You can add custom conditions too.
Each needs exactly 3 entries, and at least one must be `human`.

### Experiment parameters

| key | default | meaning |
|---|---|---|
| `experiment.rounds` | 2 | number of chat + vote rounds |
| `experiment.chat_duration_seconds` | 60 | chat length per round |
| `experiment.vote_duration_seconds` | 20 | maximum voting time (ends early when all have voted) |
| `experiment.results_display_seconds` | 10 | how long the between-round results pop-up stays up |
| `experiment.start_countdown_seconds` | 5 | countdown once all seats are filled |
| `experiment.randomize_seats` | true | randomly map participant types to the letters A/B/C |
| `experiment.max_message_length` | 300 | chat message length cap |
| `experiment.abort_after_all_humans_left_seconds` | 15 | grace period before a session whose humans all left is aborted |
| `experiment.topic` | … | task text shown to humans and given to the AI agents |

### AI / LLM settings

| key | default | meaning |
|---|---|---|
| `ai.provider` | `anthropic` | `anthropic` (Claude via the official SDK) or `rules` (offline, context-aware) |
| `ai.model` | `claude-opus-5-5` | any Claude model ID |
| `ai.effort` | `low` | `low`…`max`; `low` keeps chat replies quick |
| `ai.max_tokens` | 4000 | per-request output cap |
| `ai.use_fallbacks` | true | server-side model fallback if a request is declined |
| `ai.api_key_env` | `ANTHROPIC_API_KEY` | name of the env var that holds the key |
| `ai.timing.*` | see file | join delay, typing speed, read delay, gaps, votes |
| `ai.personas` | 3 styles | one is assigned at random to each AI seat |

## Data persistence

Everything is stored in MongoDB (database `human_ai_experiment`, kept in the
`mongo_data` volume):

| collection | contents |
|---|---|
| `sessions` | condition, seat → participant type, join/leave times, config snapshot, status (`finished`, `aborted`, `abandoned`), abort reason, start/end times, final result |
| `messages` | `session_id, id, round, sender, text, ts` |
| `votes` | `session_id, round, voter, target, ts` (unique per voter per round) |
| `results` | per-round tallies (`kind: "round"`) and the final outcome (`kind: "final"`) |

**Researcher export.** Set `ADMIN_TOKEN` in `.env` to enable these endpoints:

```bash
curl -H "X-Admin-Token: $ADMIN_TOKEN" localhost:3000/api/admin/sessions
curl -H "X-Admin-Token: $ADMIN_TOKEN" localhost:3000/api/admin/sessions/<session_id>
```

They return 404 unless the token matches, because they show participant types.
You can also query the database directly with
`docker compose exec mongo mongosh human_ai_experiment`.

## Winner calculation

Implemented in [`backend/app/winner.py`](backend/app/winner.py) as a pure
function and tested in [`backend/tests/test_winner.py`](backend/tests/test_winner.py):

1. **Round 2 decides.** The player with the most round 2 votes wins.
2. **Round 2 tie.** Only the tied players are considered. Their votes from both
   rounds are added up, and the highest total wins.
3. **Still tied.** One of the remaining tied players is picked at random.

The tests cover each rule. They also check that a player who was not tied in
round 2 is never considered in rules 2 or 3, even with a strong round 1 result,
and that the random pick only ever chooses among the players still tied.

## Design decisions

### Why MongoDB
The data is a set of append-only event logs (messages, votes) plus one nested
document per session (seat map, config snapshot, result). These map directly
onto documents. Storing the full config snapshot with each session as-is means
the schema can change between experiments without migrations. Analysis needs
are simple (filter by `session_id`, export), so relational joins add nothing
here. Writes go through an ordered background queue, so the database never
slows a live session down. If MongoDB is unreachable, writes are retried and
saved when it returns (for up to 5 minutes, configurable with
`DB_WRITE_RETRY_SECONDS`).

### Anonymity: how human and AI are kept indistinguishable
- **Neutral identities.** Participants are only Player A/B/C. When
  `randomize_seats` is on, participant types are randomly mapped to letters in
  every session, so "the AI is always C" cannot be learned.
- **No type in any response.** The WebSocket state lists seats only as
  `{id, occupied}`. Participant types live only in the server's memory and in
  the database. The only endpoints that expose them are the admin endpoints,
  which require a token.
- **One code path.** AI agents post chat and votes through the same
  `Session.post_message` / `cast_vote` methods as humans. Payloads are
  identical, IDs are random UUIDs, and all timestamps are set by the server.
- **Human-like timing.** AI seats join after a random delay instead of
  instantly. When all humans are already present, the AI still joins after a
  short random lag. Before a message is shown, the agent waits a "reading"
  pause plus a typing time based on message length (LLM latency counts toward
  it). It keeps a minimum gap between its own messages, has a per-round message
  cap, and sometimes stays quiet. Votes arrive after a random delay, always
  before the deadline.
- **Human-like content.** Each agent gets a persona and instructions to write
  short, casual messages with no markdown, lists or em dashes. If someone asks
  whether anyone is a bot, it reacts like a person would. Replies are cleaned
  before posting: "Player X:" prefixes, quotes and dashes are removed, and
  anything that mentions being an AI is dropped.
- **No typing indicators and no vote-progress counter.** Both would expose
  timing patterns that differ between humans and agents.

### Real-time architecture
- A single WebSocket per participant (`/ws`) carries chat messages and
  full-state snapshots (phase, round, deadline, seat occupancy, results). The
  client is a pure renderer of server state, so reconnecting means receiving
  the snapshot and the history again.
- The server owns all timers. Deadlines are sent as absolute timestamps along
  with `server_time`, so client countdowns are correct even when the client's
  clock is off.
- `SessionManager` routes each arriving human into the open waiting session
  and creates one lazily. Once a session starts, the next arrival opens a new
  one, so several groups can run concurrently.

### AI agents
- Provider: Claude through the official `anthropic` Python SDK (async client),
  with `output_config.effort` set from config. Server-side fallbacks are
  enabled so a declined request is retried on a fallback model. A refusal or
  API error makes the agent stay silent for that turn instead of crashing. If
  an agent fails to produce a vote, it votes at random.
- **No repeats.** Before an AI message is posted it is compared with every
  message already in the chat (ignoring case and punctuation, with a fuzzy
  match). If it is the same or very similar to anything already said, by any
  player, it isn't sent. With Claude the agent asks once for a new message;
  otherwise it stays quiet for that turn.
- **Offline `rules` responder** ([`responder_rules.py`](backend/app/responder_rules.py)).
  Used when there's no API key. Each AI seat gets a disposition (keen,
  easygoing or careful) and keeps its own state. It reads new messages and
  picks up on:
  - greetings
  - people volunteering or declining
  - nominations, including of itself
  - questions aimed at it, such as "player b what do you think?"
  - "why" questions, which it answers with a reason
  - questions about the role or how to decide
  - "are you a bot?"
  - last round's result, at the start of round 2

  Its vote follows the stance it took in the chat. Questions aimed at it that
  arrive while it is "typing" are queued and answered next. Two AI seats never
  use the same wording, and an AI usually lets the addressed player answer.
  Because it is template-based, a determined participant may still notice
  patterns, so use `anthropic` for real data collection.

## Trade-offs and known limitations

- **Live session state is kept in memory** in one backend process. That keeps
  the code simple and fast, but restarting the backend ends any session in
  progress (stored data is kept), and the backend can't be scaled to several
  instances without moving that state to shared storage such as Redis.
- **Offline AI vs. real LLM.** Without an API key, the AI seats use the
  template-based `rules` responder. It is relevant and consistent, but a
  determined participant may notice patterns. The Claude path
  (`provider: anthropic`) is implemented through the official SDK but was not
  exercised end to end during development, because no API key was configured.
  Use it for real data collection.
- **Anonymity is enforced at the protocol level** (no participant type in any
  payload, server timestamps, random seat letters, humanised AI timing). It
  cannot stop participants comparing notes outside the app.
- **No participant authentication.** Each browser tab gets an anonymous
  reconnect token, which is enough for a lab or online study link but not
  for identifying participants.
- **Missing votes count as abstentions,** so a round's tally can be below 3.
  The winner rules are applied to whatever votes were cast.
- **Generalised winner rule.** The rules are specified for 2 rounds. For other
  values of `rounds`, the last round decides and a tie is broken by summing all
  rounds.

## AI usage

Every prompt I gave the AI assistant, plus the exact model and tools used, is
in [`ai_prompts.md`](ai_prompts.md).

## Project layout

```
human_ai_experiment/
├── config.yml               experiment + condition + LLM settings
├── docker-compose.yml
├── .env.example
├── backend/                 FastAPI app (Python 3.12)
│   ├── app/main.py          WebSocket + REST endpoints
│   ├── app/session.py       session engine: waiting room, rounds, voting, results
│   ├── app/agents.py        AI agents, timing, anti-repeat, Claude provider
│   ├── app/responder_rules.py  offline context-aware responder (provider: rules)
│   ├── app/winner.py        winner calculation (pure)
│   ├── app/config.py        config.yml schema/validation
│   ├── app/db.py            MongoDB persistence
│   └── tests/               winner logic, no-repeat, offline responder tests
├── frontend/                React (Vite) app, served by nginx (which also proxies /api and /ws)
├── database/                MongoDB image + init script (collections, indexes)
└── e2e/                     end-to-end scenario matrix (runs against docker compose)
```

## Development without Docker

```bash
# MongoDB on localhost:27017 is required
cd backend && pip install -r requirements.txt
CONFIG_PATH=../config.yml MONGO_URI=mongodb://localhost:27017 uvicorn app.main:app --reload
cd frontend && npm install && npm run dev     # http://localhost:5173 (proxies to :8000)
```
