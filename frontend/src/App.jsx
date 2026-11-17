import React, { useEffect, useRef, useState } from "react";
import { useCountdown, useExperiment } from "./useExperiment.js";

const label = (id) => `Player ${id}`;

function Seats({ seats, you }) {
  return (
    <div className="seats">
      {seats.map((s) => (
        <div key={s.id} className={`seat ${s.occupied ? "filled" : "empty"} ${s.id === you ? "you" : ""}`}>
          <div className="avatar">{s.occupied ? s.id : ""}</div>
          <div className="seat-name">{label(s.id)}</div>
          <div className="seat-status">
            {s.id === you ? "You" : s.occupied ? "Joined" : "Waiting…"}
          </div>
        </div>
      ))}
    </div>
  );
}

function WaitingRoom({ state, secondsLeft }) {
  const filled = state.seats.filter((s) => s.occupied).length;
  return (
    <div className="card center">
      <h1>Waiting room</h1>
      <p className="muted">
        You are <strong>{label(state.you)}</strong>. The discussion starts when all three seats are filled.
      </p>
      <Seats seats={state.seats} you={state.you} />
      {state.phase === "starting" ? (
        <p className="big">Starting in {secondsLeft ?? 0}s…</p>
      ) : (
        <p className="muted">{filled} of 3 participants present</p>
      )}
    </div>
  );
}

function Tally({ tally, highlight }) {
  const max = Math.max(1, ...Object.values(tally));
  return (
    <div className="tally">
      {Object.entries(tally).map(([id, n]) => (
        <div key={id} className={`tally-row ${id === highlight ? "winner" : ""}`}>
          <span className="tally-name">{label(id)}</span>
          <span className="bar"><span style={{ width: `${(n / max) * 100}%` }} /></span>
          <span className="tally-count">{n} {n === 1 ? "vote" : "votes"}</span>
        </div>
      ))}
    </div>
  );
}

function Modal({ children }) {
  return (
    <div className="modal-backdrop">
      <div className="modal">{children}</div>
    </div>
  );
}

function ChatLog({ messages, you }) {
  const endRef = useRef(null);
  useEffect(() => {
    // Braces matter: scrollIntoView may return a Promise, which React would treat as a cleanup fn.
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages.length]);
  let lastRound = null;
  return (
    <div className="chat-log">
      {messages.length === 0 && <p className="muted center">No messages yet. Say hello!</p>}
      {messages.map((m) => {
        const divider = m.round !== lastRound;
        lastRound = m.round;
        return (
          <React.Fragment key={m.id}>
            {divider && <div className="divider">Round {m.round}</div>}
            <div className={`msg ${m.sender === you ? "mine" : ""}`}>
              <span className="sender">{m.sender === you ? "You" : label(m.sender)}</span>
              <span className="bubble">{m.text}</span>
            </div>
          </React.Fragment>
        );
      })}
      <div ref={endRef} />
    </div>
  );
}

function VotePanel({ state, onVote }) {
  return (
    <div className="vote-panel">
      <h2>Vote: who should be the Decision Maker?</h2>
      {state.my_vote ? (
        <p>You voted for <strong>{label(state.my_vote)}</strong>. Waiting for the others…</p>
      ) : (
        <div className="vote-buttons">
          {state.seats.map((s) => (
            <button key={s.id} onClick={() => onVote(s.id)}>
              {label(s.id)}{s.id === state.you ? " (you)" : ""}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

const METHOD_TEXT = {
  final_round_majority: "Most votes in the final round.",
  total_votes_tiebreak: "Final round was tied; decided by total votes across all rounds among the tied players.",
  random_tiebreak: "Still tied after counting all rounds; chosen at random among the tied players.",
};

function Discussion({ state, messages, secondsLeft, sendChat, vote }) {
  const [draft, setDraft] = useState("");
  const canChat = state.phase === "chat";
  const submit = (e) => {
    e.preventDefault();
    const text = draft.trim();
    if (!text || !canChat) return;
    sendChat(text);
    setDraft("");
  };
  const phaseText = { chat: "Discussion", vote: "Voting", results: "Results" }[state.phase] || "";

  return (
    <div className="layout">
      <aside className="card side">
        <h3>Participants</h3>
        <ul className="people">
          {state.seats.map((s) => (
            <li key={s.id}>
              <span className="dot" /> {label(s.id)} {s.id === state.you && <em>(you)</em>}
            </li>
          ))}
        </ul>
        <h3>Task</h3>
        <p className="muted small">{state.topic}</p>
      </aside>

      <main className="card main">
        <header className="topbar">
          <div>
            <strong>Round {state.round} of {state.total_rounds}</strong> · {phaseText}
          </div>
          {secondsLeft !== null && <div className={`timer ${secondsLeft <= 10 ? "low" : ""}`}>{secondsLeft}s</div>}
        </header>

        <ChatLog messages={messages} you={state.you} />

        {state.phase === "vote" ? (
          <VotePanel state={state} onVote={vote} />
        ) : (
          <form className="composer" onSubmit={submit}>
            <input
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder={canChat ? "Type a message…" : "Chat is closed"}
              maxLength={state.max_message_length}
              disabled={!canChat}
              autoFocus
            />
            <button type="submit" disabled={!canChat || !draft.trim()}>Send</button>
          </form>
        )}
      </main>

      {state.phase === "results" && state.round_results.length > 0 && (
        <Modal>
          <h2>Round {state.round} results</h2>
          <Tally tally={state.round_results[state.round_results.length - 1].tally} />
          <p className="muted">Round {state.round + 1} begins in {secondsLeft ?? 0}s.</p>
        </Modal>
      )}
    </div>
  );
}

function FinalResults({ state, leave }) {
  const last = state.round_results[state.round_results.length - 1];
  const f = state.final;
  return (
    <Modal>
      <h2>Final results</h2>
      <p className="muted">Round {last.round} votes</p>
      <Tally tally={last.tally} highlight={f.winner} />
      <div className="elected">
        Elected Decision Maker: <strong>{label(f.winner)}</strong>
        {f.winner === state.you && " (you)"}
      </div>
      <p className="muted small">{METHOD_TEXT[f.method]}</p>
      <button onClick={leave}>Finish</button>
    </Modal>
  );
}

export default function App() {
  const { state, messages, connected, clockOffset, sendChat, vote, leave } = useExperiment();
  const secondsLeft = useCountdown(state?.deadline, clockOffset);

  let body;
  if (!state) {
    body = <div className="card center"><p>Connecting…</p></div>;
  } else if (state.phase === "aborted") {
    body = (
      <div className="card center">
        <h1>Session ended</h1>
        <p className="muted">This session was ended because participants left before it finished.</p>
        <button onClick={leave}>Back to start</button>
      </div>
    );
  } else if (state.phase === "waiting" || state.phase === "starting") {
    body = <WaitingRoom state={state} secondsLeft={secondsLeft} />;
  } else {
    body = (
      <>
        <Discussion state={state} messages={messages} secondsLeft={secondsLeft} sendChat={sendChat} vote={vote} />
        {state.phase === "finished" && state.final && <FinalResults state={state} leave={leave} />}
      </>
    );
  }

  return (
    <div className="app">
      {!connected && state && <div className="banner">Reconnecting…</div>}
      {body}
    </div>
  );
}
