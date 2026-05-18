import React, { useEffect, useRef, useState } from "react";
import { createSocketClient } from "./lib/socket.js";

const DEFAULT_STATUS = "Connecting to lobby service...";
const HEX_R = 34;
const HEX_W = Math.sqrt(3) * HEX_R;
const HEX_H = HEX_R * 2;
const ANSWER_LABELS = ["A", "B", "C", "D"];
const QUESTION_TYPE_LABELS = {
  multiple_choice: "Multiple Choice",
  true_false: "True or False",
  estimate: "Estimate",
};
const SOUND_PATHS = {
  button: "/sounds/button-click.mp3",
  countdown: "/sounds/lobby-start-countdown.mp3",
  question: "/sounds/question-start.mp3",
  selection: "/sounds/selection-lock.mp3",
  territory: "/sounds/territory-claim.mp3",
  correct: "/sounds/answer-correct.mp3",
  wrong: "/sounds/answer-wrong.mp3",
  finish: "/sounds/match-finished.mp3",
};
const INITIAL_FORM_STATE = {
  displayName: "",
  createLobbyName: "",
  joinLobbyName: "",
  categories: [],
  difficulties: [],
  questionTypes: [],
  gameSpeed: 20,
  mapRadius: 3,
  battleRounds: 12,
};

function hexPoints(cx, cy, radius = HEX_R) {
  return Array.from({ length: 6 }, (_, index) => {
    const angle = (Math.PI / 3) * index - Math.PI / 6;
    return `${(cx + radius * Math.cos(angle)).toFixed(2)},${(cy + radius * Math.sin(angle)).toFixed(2)}`;
  }).join(" ");
}

function hexCenter(q, r) {
  return {
    cx: HEX_W * (q + r / 2),
    cy: HEX_R * 1.5 * r,
  };
}

function buildBoardLayout(cells) {
  if (!cells.length) {
    return { cells: [], width: 240, height: 240 };
  }
  const positioned = cells.map((cell) => {
    const center = hexCenter(cell.q, cell.r);
    return { ...cell, ...center };
  });
  const minX = Math.min(...positioned.map((cell) => cell.cx - HEX_W / 2));
  const maxX = Math.max(...positioned.map((cell) => cell.cx + HEX_W / 2));
  const minY = Math.min(...positioned.map((cell) => cell.cy - HEX_H / 2));
  const maxY = Math.max(...positioned.map((cell) => cell.cy + HEX_H / 2));
  const pad = 28;
  return {
    cells: positioned.map((cell) => ({
      ...cell,
      cx: cell.cx - minX + pad,
      cy: cell.cy - minY + pad,
    })),
    width: maxX - minX + pad * 2,
    height: maxY - minY + pad * 2,
  };
}

function buildAdjacency(cells) {
  const byCoord = new Map(cells.map((cell) => [`${cell.q},${cell.r}`, cell.territory_id]));
  const directions = [
    [1, 0],
    [1, -1],
    [0, -1],
    [-1, 0],
    [-1, 1],
    [0, 1],
  ];
  const adjacency = {};
  cells.forEach((cell) => {
    adjacency[cell.territory_id] = directions
      .map(([dq, dr]) => byCoord.get(`${cell.q + dq},${cell.r + dr}`))
      .filter(Boolean);
  });
  return adjacency;
}

function formatCountdown(seconds) {
  const safe = Math.max(0, seconds);
  return `${String(Math.floor(safe / 60)).padStart(2, "0")}:${String(safe % 60).padStart(2, "0")}`;
}

function normalizeMatch(rawMatch) {
  if (!rawMatch) return null;
  return {
    id: rawMatch.id,
    currentState: rawMatch.current_state,
    mapData: rawMatch.map_data || {},
    resultData: rawMatch.result_data || {},
    resultView: rawMatch.result_view || rawMatch.result_data?.result_view || null,
    playerIds: rawMatch.player_ids || [],
    players: rawMatch.players || {},
    settings: rawMatch.settings || {},
    lobbyName: rawMatch.lobby_name || null,
    connectedPlayerIds: rawMatch.connected_player_ids || [],
  };
}

function toggleValue(list, value) {
  return list.includes(value) ? list.filter((entry) => entry !== value) : [...list, value];
}

function chunkItems(items, size) {
  const chunks = [];
  for (let index = 0; index < items.length; index += size) {
    chunks.push(items.slice(index, index + size));
  }
  return chunks;
}

function formatDifficultyLabel(value) {
  if (!value) return "Any difficulty";
  return value.charAt(0).toUpperCase() + value.slice(1);
}

function formatDifficultyList(values) {
  if (!values?.length) return "Any difficulty";
  return values.map((value) => formatDifficultyLabel(value)).join(", ");
}

function formatQuestionTypeList(values) {
  if (!values?.length) return "Any type";
  return values.map((value) => QUESTION_TYPE_LABELS[value] || value).join(", ");
}

function formatMapSizeLabel(value) {
  if (value === 2) return "Small map";
  if (value === 4) return "Large map";
  return "Medium map";
}

function formatBattleRoundsLabel(value) {
  if (value === 6) return "Quick - 6";
  if (value === 18) return "Long - 18";
  return "Average - 12";
}

function formatGameSpeedLabel(value) {
  if (value === 10) return "Fast - 10s";
  if (value === 30) return "Slow - 30s";
  return "Average - 20s";
}

function EventTicker({ events }) {
  return (
    <div className="event-strip">
      {events.slice(0, 8).map((event) => (
        <span key={event.id} className="event-pill">
          <strong>{event.type}</strong> {event.message}
        </span>
      ))}
    </div>
  );
}

function PlayerPanel({ title, playerName, territories, baseHp, active }) {
  return (
    <div className={`player-panel ${active ? "panel-active" : ""}`}>
      <span className="panel-kicker">{title}</span>
      <strong>{playerName || "Waiting..."}</strong>
      <div className="panel-metrics">
        <span>{territories} hexes</span>
        <span>{baseHp} castle HP</span>
      </div>
    </div>
  );
}

function SelectionBanner({ selectionPrompt, localPlayerId, playerLookup }) {
  if (!selectionPrompt) return null;
  const actor = playerLookup[String(selectionPrompt.activePlayerId)];
  return (
    <div className={`selection-banner ${selectionPrompt.activePlayerId === localPlayerId ? "your-turn" : "other-turn"}`}>
      <strong>{selectionPrompt.activePlayerId === localPlayerId ? "Your move" : `${actor?.name || "Opponent"} is choosing`}</strong>
      <span>{selectionPrompt.message}</span>
    </div>
  );
}

function HexBoard({
  cells,
  localPlayerId,
  validTargets,
  onSelect,
  selectionEnabled,
  winnerId,
  highlights,
  attackedTerritoryId,
}) {
  const layout = buildBoardLayout(cells);
  const validTargetSet = new Set(validTargets || []);
  const adjacency = buildAdjacency(cells);

  return (
    <div className="board-shell">
      <svg viewBox={`0 0 ${layout.width} ${layout.height}`} className="board-svg" xmlns="http://www.w3.org/2000/svg">
        <defs>
          <filter id="glow-blue" x="-60%" y="-60%" width="220%" height="220%">
            <feGaussianBlur stdDeviation="3" result="blur" />
            <feMerge>
              <feMergeNode in="blur" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>
          <filter id="glow-red" x="-60%" y="-60%" width="220%" height="220%">
            <feGaussianBlur stdDeviation="3" result="blur" />
            <feMerge>
              <feMergeNode in="blur" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>
          <filter id="glow-gold" x="-60%" y="-60%" width="220%" height="220%">
            <feGaussianBlur stdDeviation="4" result="blur" />
            <feMerge>
              <feMergeNode in="blur" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>
        </defs>

        {layout.cells.flatMap((cell) =>
          (adjacency[cell.territory_id] || [])
            .filter((neighborId) => neighborId > cell.territory_id)
            .map((neighborId) => {
              const neighbor = layout.cells.find((entry) => entry.territory_id === neighborId);
              if (!neighbor) return null;
              return (
                <line
                  key={`${cell.territory_id}-${neighborId}`}
                  x1={cell.cx}
                  y1={cell.cy}
                  x2={neighbor.cx}
                  y2={neighbor.cy}
                  stroke="rgba(164, 184, 212, 0.12)"
                  strokeWidth="1"
                />
              );
            })
        )}

        {layout.cells.map((cell) => {
          const ownedByLocal = cell.owner_id === localPlayerId;
          const ownedByOpponent = cell.owner_id != null && cell.owner_id !== localPlayerId;
          const fill = ownedByLocal ? (cell.is_base ? "#2362e0" : "#183f7d") : ownedByOpponent ? (cell.is_base ? "#ad2f39" : "#6a1f29") : "#161f31";
          const stroke = ownedByLocal ? "#7bc7ff" : ownedByOpponent ? "#ff9ca4" : "#4b5675";
          const effect = highlights[cell.territory_id];
          const selectable = selectionEnabled && validTargetSet.has(cell.territory_id);
          const isWinnerHex = winnerId != null && cell.owner_id === winnerId;
          const isAttackedHex = attackedTerritoryId != null && cell.territory_id === attackedTerritoryId;
          const filter = isWinnerHex ? "url(#glow-gold)" : ownedByLocal && cell.is_base ? "url(#glow-blue)" : ownedByOpponent && cell.is_base ? "url(#glow-red)" : undefined;
          const pipStartX = cell.cx - ((cell.hp - 1) * 8) / 2;
          return (
            <g
              key={cell.territory_id}
              className={selectable ? "hex-selectable" : ""}
              onClick={() => selectable && onSelect(cell.territory_id)}
            >
              <polygon
                points={hexPoints(cell.cx, cell.cy)}
                fill={fill}
                stroke={effect || isAttackedHex ? "#f5c451" : stroke}
                strokeWidth={selectable || effect || isAttackedHex ? 3 : 1.7}
                filter={filter}
              />
              {isAttackedHex && (
                <polygon
                  points={hexPoints(cell.cx, cell.cy, HEX_R - 7)}
                  fill="rgba(245, 196, 81, 0.16)"
                  stroke="#ffe7a3"
                  strokeWidth="2"
                  strokeDasharray="7 5"
                />
              )}
              {selectable && (
                <polygon
                  points={hexPoints(cell.cx, cell.cy, HEX_R - 4)}
                  fill="none"
                  stroke="#f5c451"
                  strokeWidth="1.4"
                  strokeDasharray="5 4"
                />
              )}
              <text x={cell.cx} y={cell.cy - 9} textAnchor="middle" fill="#eef3ff" fontSize="11" fontWeight="700">
                {cell.territory_id}
              </text>
              {cell.is_base && (
                <text x={cell.cx} y={cell.cy + 6} textAnchor="middle" fill="#fff1b0" fontSize="17">
                  🏰
                </text>
              )}
              {Array.from({ length: cell.hp }, (_, index) => (
                <circle
                  key={index}
                  cx={pipStartX + index * 8}
                  cy={cell.cy + 16}
                  r="2.8"
                  fill={ownedByOpponent ? "#ffb4bb" : ownedByLocal ? "#9fd9ff" : "#8b97b6"}
                />
              ))}
            </g>
          );
        })}
      </svg>
    </div>
  );
}

function QuestionPanel({
  question,
  phase,
  countdown,
  showCountdown,
  onAnswer,
  lastResult,
  answerFeedback,
  pauseMessage,
  disabled,
}) {
  const [typedAnswer, setTypedAnswer] = useState("");
  const acceptsTypedAnswer = question?.question_type === "estimate";

  useEffect(() => {
    setTypedAnswer("");
  }, [question?.id]);

  function renderLastResult() {
    if (!lastResult) return null;
    return (
      <div className="result-box">
        <strong>{lastResult.title}</strong>
        {Array.isArray(lastResult.lines) && lastResult.lines.length > 0 ? (
          <div className="result-lines">
            {lastResult.lines.map((line, index) => (
              <span key={`${lastResult.title}-${index}`}>{line}</span>
            ))}
          </div>
        ) : (
          <span>{lastResult.body}</span>
        )}
      </div>
    );
  }

  if (!question && pauseMessage) {
    return (
      <section className="question-panel idle-panel">
        <div className="question-head">
          <span className="phase-chip">{(phase || "pause").replaceAll("_", " ")}</span>
          {showCountdown && <span className="countdown-readout">{countdown.label}</span>}
        </div>
        <h3>{pauseMessage}</h3>
        <p>The next question will start automatically.</p>
        {answerFeedback && (
          <div className={`feedback-toast feedback-${answerFeedback.kind}`}>
            <strong>{answerFeedback.title}</strong>
            <span>{answerFeedback.body}</span>
          </div>
        )}
        {renderLastResult()}
      </section>
    );
  }

  if (!question) {
    return (
      <section className="question-panel idle-panel">
        <span className="phase-chip">{(phase || "lobby").replaceAll("_", " ")}</span>
        <h3>Waiting for the next prompt</h3>
        <p>The server will push selection turns and questions here.</p>
        {answerFeedback && (
          <div className={`feedback-toast feedback-${answerFeedback.kind}`}>
            <strong>{answerFeedback.title}</strong>
            <span>{answerFeedback.body}</span>
          </div>
        )}
        {renderLastResult()}
      </section>
    );
  }

  return (
    <section className="question-panel">
      <div className="question-head">
        <span className="phase-chip">{phase.replaceAll("_", " ")}</span>
        {showCountdown && <span className="countdown-readout">{countdown.label}</span>}
      </div>
      <h3>{question.text}</h3>
      <p className="question-meta">
        {question.category || "Mixed"} . {question.question_type.replaceAll("_", " ")}
      </p>
      {showCountdown && (
        <div className="timer-track">
          <div className="timer-fill" style={{ width: `${countdown.progress * 100}%` }} />
        </div>
      )}
      {answerFeedback && (
        <div className={`feedback-toast feedback-${answerFeedback.kind}`}>
          <strong>{answerFeedback.title}</strong>
          <span>{answerFeedback.body}</span>
        </div>
      )}
      {Array.isArray(question.options) && question.options.length > 0 && (
        <div className="answer-list">
          {question.options.map((option, index) => (
            <button key={option} type="button" className="answer-card" onClick={() => onAnswer(option)} disabled={disabled}>
              <span className="answer-key">{ANSWER_LABELS[index] || index + 1}</span>
              <span>{option}</span>
            </button>
          ))}
        </div>
      )}
      {acceptsTypedAnswer && (
        <div className="manual-answer">
          <input
            value={typedAnswer}
            onChange={(event) => setTypedAnswer(event.target.value)}
            placeholder="Type an answer"
          />
          <button type="button" onClick={() => onAnswer(typedAnswer)} disabled={disabled || !typedAnswer.trim()}>
            Send
          </button>
        </div>
      )}
      {renderLastResult()}
    </section>
  );
}

function ActiveLobbyList({ lobbies, onJoin }) {
  return (
    <section className="glass-panel directory-card">
      <span className="eyebrow">Active Lobbies</span>
      <h2>Open rooms</h2>
      {lobbies.length === 0 ? (
        <p className="directory-empty">No active lobbies right now.</p>
      ) : (
        <div className="directory-list">
          {lobbies.map((entry) => (
            <div key={entry.id} className="directory-row">
              <div>
                <strong>{entry.name}</strong>
                <span>{entry.host?.name || "Host"} vs {entry.guest?.name || "Waiting for player 2"}</span>
              </div>
              <div className="directory-meta">
                <span>{entry.state.replaceAll("_", " ")}</span>
                <span>{formatMapSizeLabel(entry.settings?.map_radius || 3)}</span>
                <span>{formatBattleRoundsLabel(entry.settings?.battle_rounds || 12)}</span>
                <span>{formatGameSpeedLabel(entry.settings?.timer_seconds || 20)}</span>
              </div>
              <button type="button" className="secondary-btn" onClick={() => onJoin(entry.name)} disabled={!entry.can_join}>
                {entry.can_join ? "Join" : "Busy"}
              </button>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function IdentityAndLobbyForm({
  formState,
  setFormState,
  options,
  onCreateLobby,
  onJoinLobby,
  onShowActiveLobbies,
  onResetIdentity,
  onDisplayNameChange,
  nameValidation,
  status,
  connected,
}) {
  const categories = options.categories || [];
  const questionTypes = options.question_types || [];
  const difficulties = options.difficulties || [];
  const mapSizes = options.map_sizes || [];
  const battleRoundOptions = options.battle_round_options || [];
  const gameSpeedOptions = options.game_speed_options || [];
  const categoryRows = chunkItems(categories, 3);
  const difficultyRows = chunkItems(difficulties, 3);
  const questionTypeRows = chunkItems(questionTypes, 3);
  const [openQuestionTypeInfo, setOpenQuestionTypeInfo] = useState(null);
  const activeQuestionTypeInfo = questionTypes.find((entry) => entry.value === openQuestionTypeInfo)?.info_text;

  return (
    <div className="home-grid">
      <section className="identity-card glass-panel compact-panel">
        <span className="eyebrow">Identity</span>
        <label>
          <span>Displayed name</span>
          <input
            value={formState.displayName}
            onChange={(event) => onDisplayNameChange(event.target.value)}
            placeholder="Pick a visible name"
            className={nameValidation.state === "error" ? "input-error" : ""}
          />
        </label>
        {nameValidation.message ? (
          <p className={`field-hint name-validation-copy name-validation-${nameValidation.state}`}>
            {nameValidation.message}
          </p>
        ) : null}
        <p className="status-copy menu-status-copy">{connected ? status : "Waiting for websocket connection..."}</p>
        <div className="stack-actions identity-actions">
          <button type="button" className="secondary-btn" onClick={onResetIdentity} disabled={!connected}>
            Reset
          </button>
        </div>
      </section>

      <section className="glass-panel join-card compact-panel">
        <span className="eyebrow">Join Lobby</span>
        <label>
          <span>Lobby name</span>
          <input
            value={formState.joinLobbyName}
            onChange={(event) => setFormState((current) => ({ ...current, joinLobbyName: event.target.value }))}
            placeholder="Enter an existing room"
          />
        </label>
        <div className="stack-actions">
          <button type="button" className="secondary-btn" onClick={onJoinLobby} disabled={!connected || nameValidation.state === "checking"}>
            Join Lobby
          </button>
          <button type="button" className="secondary-btn" onClick={onShowActiveLobbies} disabled={!connected}>
            Show Active Lobbies
          </button>
        </div>
      </section>

      <section className="glass-panel lobby-builder compact-panel">
        <span className="eyebrow">Create Lobby</span>
        <label>
          <span>Lobby name</span>
          <input
            value={formState.createLobbyName}
            onChange={(event) => setFormState((current) => ({ ...current, createLobbyName: event.target.value }))}
            placeholder="for example: history-arena"
          />
        </label>
        <div>
          <span className="field-label">Categories</span>
          <div className="category-table-wrap">
            <table className="category-table">
              <tbody>
                {categoryRows.map((row, index) => (
                  <tr key={index}>
                    {row.map((category) => (
                      <td key={category.value}>
                        <label className="category-checkbox-row">
                          <input
                            type="checkbox"
                            checked={formState.categories.includes(category.value)}
                            onChange={() => setFormState((current) => ({ ...current, categories: toggleValue(current.categories, category.value) }))}
                          />
                          <span>{category.label}</span>
                        </label>
                      </td>
                    ))}
                    {Array.from({ length: 3 - row.length }, (_, fillerIndex) => <td key={`category-empty-${index}-${fillerIndex}`} />)}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
        <div>
          <span className="field-label">Difficulty</span>
          <div className="category-table-wrap compact-table-wrap">
            <table className="category-table compact-table">
              <tbody>
                {difficultyRows.map((row, index) => (
                  <tr key={index}>
                    {row.map((entry) => (
                      <td key={entry.value}>
                        <label className="category-checkbox-row">
                          <input
                            type="checkbox"
                            checked={formState.difficulties.includes(entry.value)}
                            onChange={() => setFormState((current) => ({ ...current, difficulties: toggleValue(current.difficulties, entry.value) }))}
                          />
                          <span>{entry.label}</span>
                        </label>
                      </td>
                    ))}
                    {Array.from({ length: 3 - row.length }, (_, fillerIndex) => <td key={`difficulty-empty-${index}-${fillerIndex}`} />)}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
        <div>
          <div className="inline-field-header">
            <span className="field-label">Question Type</span>
          </div>
          <div className="category-table-wrap compact-table-wrap">
            <table className="category-table compact-table">
              <tbody>
                {questionTypeRows.map((row, index) => (
                  <tr key={index}>
                    {row.map((entry) => (
                      <td key={entry.value}>
                        <label className="category-checkbox-row">
                          <input
                            type="checkbox"
                            checked={formState.questionTypes.includes(entry.value)}
                            onChange={() => setFormState((current) => ({ ...current, questionTypes: toggleValue(current.questionTypes, entry.value) }))}
                          />
                          <span>{entry.label}</span>
                          {entry.info_text && (
                            <button
                              type="button"
                              className="info-btn"
                              onClick={(event) => {
                                event.preventDefault();
                                setOpenQuestionTypeInfo((current) => current === entry.value ? null : entry.value);
                              }}
                              aria-label={`${entry.label} question info`}
                            >
                              i
                            </button>
                          )}
                        </label>
                      </td>
                    ))}
                    {Array.from({ length: 3 - row.length }, (_, fillerIndex) => <td key={`type-empty-${index}-${fillerIndex}`} />)}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {activeQuestionTypeInfo && <pre className="estimate-info-popover">{activeQuestionTypeInfo}</pre>}
        </div>
        <label>
          <span>Game speed</span>
          <select
            value={formState.gameSpeed}
            onChange={(event) => setFormState((current) => ({ ...current, gameSpeed: Number(event.target.value) }))}
          >
            {gameSpeedOptions.map((entry) => (
              <option key={entry.value} value={entry.value}>{entry.label}</option>
            ))}
          </select>
        </label>
        <label>
          <span>Hex map size</span>
          <select
            value={formState.mapRadius}
            onChange={(event) => setFormState((current) => ({ ...current, mapRadius: Number(event.target.value) }))}
          >
            {mapSizes.map((entry) => (
              <option key={entry.value} value={entry.value}>{entry.label}</option>
            ))}
          </select>
        </label>
        <label>
          <span>Max battle rounds</span>
          <select
            value={formState.battleRounds}
            onChange={(event) => setFormState((current) => ({ ...current, battleRounds: Number(event.target.value) }))}
          >
            {battleRoundOptions.map((entry) => (
              <option key={entry.value} value={entry.value}>{entry.label}</option>
            ))}
          </select>
        </label>
        <div className="builder-actions">
          <button type="button" className="primary-btn" onClick={onCreateLobby} disabled={!connected || nameValidation.state === "checking"}>
            Create Lobby
          </button>
          <p className="field-hint builder-hint">Leave all unchecked for any setting.</p>
        </div>
      </section>
    </div>
  );
}

function LobbyDirectoryPage({ lobbies, onJoin, onBack }) {
  return (
    <section className="directory-page">
      <ActiveLobbyList lobbies={lobbies} onJoin={onJoin} />
      <div className="room-actions">
        <button type="button" className="secondary-btn" onClick={onBack}>
          Back
        </button>
      </div>
    </section>
  );
}

function FinalStandWaitingPage({ lobbyName, countdownLabel }) {
  return (
    <section className="glass-panel results-panel final-stand-page">
      <span className="eyebrow">Final Stand</span>
      <h2>{lobbyName || "Match"}</h2>
      <p className="status-copy">Opening final results in {countdownLabel}.</p>
    </section>
  );
}

function FinalStandPage({ leaderboard, resultView, playerLookup, title, onReturnHome }) {
  const mode = resultView?.mode || "standard";
  const visibleLeaderboard = resultView?.leaderboard || leaderboard;
  return (
    <section className="glass-panel results-panel final-stand-page">
      <span className="eyebrow">Final Stand</span>
      <h2>{title}</h2>
      {mode === "conquest" && <p className="status-copy">Ranked by home base capture. Only won questions are shown.</p>}
      {mode === "round_limit" && <p className="status-copy">Tie-break: hexes, castle HP, then won questions.</p>}
      <div className="leaderboard-list">
        {visibleLeaderboard.map((entry, index) => (
          <div key={entry.player_id} className="leaderboard-row">
            {(() => {
              const wonQuestions = entry.won_questions ?? entry.correct_answers ?? 0;
              return (
                <>
            <span>#{index + 1}</span>
            <strong>{playerLookup[String(entry.player_id)]?.name || `Player ${entry.player_id}`}</strong>
            {mode === "conquest" ? <span>{wonQuestions} won questions</span> : null}
            {mode === "round_limit" ? <span>{entry.territories} hexes</span> : null}
            {mode === "round_limit" ? <span>{entry.base_hp} castle HP</span> : null}
            {mode === "standard" ? <span>{entry.score} pts</span> : null}
            {mode === "standard" ? <span>{entry.territories} hexes</span> : null}
            {mode === "standard" ? <span>{entry.base_hp} castle HP</span> : null}
            {mode !== "conquest" ? <span>{wonQuestions} won questions</span> : null}
                </>
              );
            })()}
          </div>
        ))}
      </div>
      <div className="room-actions">
        <button type="button" className="secondary-btn" onClick={onReturnHome}>
          Return Home
        </button>
      </div>
    </section>
  );
}

function LobbyRoom({ lobby, player, onStart, onLeave, startCountdownLabel }) {
  const isHost = lobby.host?.id === player?.id;
  return (
    <div className="room-grid">
      <section className="glass-panel room-card">
        <span className="eyebrow">Lobby</span>
        <h2>{lobby.name}</h2>
        <p className="room-state">{lobby.state.replaceAll("_", " ")}</p>
        <div className="room-players">
          <div className="room-player-block">
            <span>Host</span>
            <strong>{lobby.host?.name}</strong>
          </div>
          <div className="room-player-block">
            <span>Guest</span>
            <strong>{lobby.guest?.name || "Waiting for player 2"}</strong>
          </div>
        </div>
        <div className="settings-list">
          <span>{formatMapSizeLabel(lobby.settings.map_radius)}</span>
          <span>Max battle rounds {formatBattleRoundsLabel(lobby.settings.battle_rounds)}</span>
          <span>Game speed {formatGameSpeedLabel(lobby.settings.timer_seconds || 20)}</span>
          <span>{(lobby.settings.categories || []).join(", ") || "All categories"}</span>
          <span>{formatDifficultyList(lobby.settings.difficulties || [])}</span>
          <span>{formatQuestionTypeList(lobby.settings.question_types || [])}</span>
        </div>
        {startCountdownLabel && <div className="start-countdown-banner">Starting game in {startCountdownLabel}</div>}
        <div className="room-actions">
          <button type="button" className="primary-btn" onClick={onStart} disabled={!isHost || !lobby.can_start || lobby.state === "in_match" || Boolean(startCountdownLabel)}>
            Start Game
          </button>
          <button type="button" className="secondary-btn" onClick={onLeave} disabled={lobby.state === "in_match"}>
            Leave Lobby
          </button>
        </div>
      </section>
    </div>
  );
}

export default function App() {
  const [player, setPlayer] = useState(null);
  const [options, setOptions] = useState({ categories: [], question_types: [], difficulties: [], map_sizes: [], battle_round_options: [], game_speed_options: [] });
  const [status, setStatus] = useState(DEFAULT_STATUS);
  const [connected, setConnected] = useState(false);
  const [lobby, setLobby] = useState(null);
  const [match, setMatch] = useState(null);
  const [activeLobbies, setActiveLobbies] = useState([]);
  const [homeView, setHomeView] = useState("builder");
  const [phase, setPhase] = useState("");
  const [question, setQuestion] = useState(null);
  const [roundNumber, setRoundNumber] = useState(0);
  const [selectionPrompt, setSelectionPrompt] = useState(null);
  const [events, setEvents] = useState([]);
  const [countdown, setCountdown] = useState({ label: "00:00", progress: 0, visible: false });
  const [questionWindow, setQuestionWindow] = useState(null);
  const [pauseWindow, setPauseWindow] = useState(null);
  const [pauseMessage, setPauseMessage] = useState("");
  const [winnerId, setWinnerId] = useState(null);
  const [highlights, setHighlights] = useState({});
  const [lastResult, setLastResult] = useState(null);
  const [answerFeedback, setAnswerFeedback] = useState(null);
  const [leaderboard, setLeaderboard] = useState([]);
  const [answerSubmittedRound, setAnswerSubmittedRound] = useState(null);
  const [selectionSubmittedRound, setSelectionSubmittedRound] = useState(null);
  const [matchConnected, setMatchConnected] = useState(false);
  const [matchConnectionEnabled, setMatchConnectionEnabled] = useState(true);
  const [connectedPlayerIds, setConnectedPlayerIds] = useState([]);
  const [forcedExitWindow, setForcedExitWindow] = useState(null);
  const [forcedExitCountdown, setForcedExitCountdown] = useState({ label: "00:00", progress: 0 });
  const [finalStandWindow, setFinalStandWindow] = useState(null);
  const [finalStandCountdown, setFinalStandCountdown] = useState({ label: "00:00", progress: 0 });
  const [lobbyStartWindow, setLobbyStartWindow] = useState(null);
  const [lobbyStartCountdown, setLobbyStartCountdown] = useState({ label: "00:00", progress: 0 });
  const [formState, setFormState] = useState(INITIAL_FORM_STATE);
  const [nameValidation, setNameValidation] = useState({ state: "idle", message: "" });

  const lobbySocketRef = useRef(null);
  const matchSocketRef = useRef(null);
  const matchRef = useRef(null);
  const highlightTimerRef = useRef(null);
  const leaveTimerRef = useRef(null);
  const finalStandTimerRef = useRef(null);
  const soundEffectsRef = useRef({});
  const suppressMatchCloseRef = useRef(false);
  const displayNameRef = useRef("");
  const nameCheckTargetRef = useRef("");

  useEffect(() => {
    matchRef.current = match;
  }, [match]);

  useEffect(() => {
    displayNameRef.current = formState.displayName.trim();
  }, [formState.displayName]);

  useEffect(() => {
    if (!connected) return undefined;
    const desiredName = formState.displayName.trim();
    if (desiredName.length < 2 || desiredName.length > 32) {
      nameCheckTargetRef.current = "";
      if (nameValidation.state === "checking") {
        setNameValidation({ state: "idle", message: "" });
      }
      return undefined;
    }
    if (player?.display_name === desiredName) {
      if (nameCheckTargetRef.current || nameValidation.state !== "ready") {
        nameCheckTargetRef.current = "";
        setNameValidation({ state: "ready", message: "Name ready" });
      }
      return undefined;
    }
    if (nameCheckTargetRef.current === desiredName && nameValidation.state !== "idle") {
      return undefined;
    }

    const timeoutId = window.setTimeout(() => {
      nameCheckTargetRef.current = desiredName;
      setNameValidation({ state: "checking", message: "Checking name..." });
      sendLobby({ action: "set_profile", display_name: desiredName });
    }, 1000);

    return () => window.clearTimeout(timeoutId);
  }, [connected, formState.displayName, player?.display_name, nameValidation.state]);

  function pushEvent(type, message) {
    setEvents((current) => [{ id: crypto.randomUUID(), type, message }, ...current].slice(0, 30));
  }

  function playSound(name) {
    const audio = soundEffectsRef.current[name];
    if (!audio) return;
    try {
      audio.currentTime = 0;
      const result = audio.play();
      if (result?.catch) {
        result.catch(() => {});
      }
    } catch {
      // Missing or blocked audio should not interrupt the game loop.
    }
  }

  function updatePlayer(nextPlayer) {
    if (nextPlayer?.guest_token) {
      window.sessionStorage.setItem("triviadorGuestToken", nextPlayer.guest_token);
    }
    setPlayer(nextPlayer);
    setFormState((current) => ({
      ...current,
      displayName: current.displayName || nextPlayer.display_name || "",
    }));
  }

  function handleDisplayNameChange(nextValue) {
    setFormState((current) => ({ ...current, displayName: nextValue }));
    const trimmedValue = nextValue.trim();
    nameCheckTargetRef.current = "";
    if (!trimmedValue) {
      setNameValidation({ state: "idle", message: "" });
      return;
    }
    if (trimmedValue === player?.display_name) {
      setNameValidation({ state: "ready", message: "Name ready" });
      return;
    }
    setNameValidation({ state: "idle", message: "" });
  }

  function sendLobby(payload) {
    if (!lobbySocketRef.current || lobbySocketRef.current.readyState !== WebSocket.OPEN) {
      setStatus("Lobby socket is not ready yet.");
      return false;
    }
    lobbySocketRef.current.send(JSON.stringify(payload));
    return true;
  }

  function requestLobbyDirectory() {
    if (lobbySocketRef.current?.readyState === WebSocket.OPEN) {
      lobbySocketRef.current.send(JSON.stringify({ action: "list_lobbies" }));
    }
  }

  function clearLeaveTimer() {
    if (leaveTimerRef.current) {
      window.clearTimeout(leaveTimerRef.current);
      leaveTimerRef.current = null;
    }
  }

  function clearLobbyStartTimer() {
    setLobbyStartWindow(null);
    setLobbyStartCountdown({ label: "00:00", progress: 0 });
  }

  function clearFinalStandTimer() {
    if (finalStandTimerRef.current) {
      window.clearTimeout(finalStandTimerRef.current);
      finalStandTimerRef.current = null;
    }
    setFinalStandWindow(null);
    setFinalStandCountdown({ label: "00:00", progress: 0 });
  }

  function scheduleReturnHome(nextStatus, countdownSeconds = 3) {
    clearLeaveTimer();
    const seconds = Math.max(1, Number(countdownSeconds) || 3);
    const durationMs = seconds * 1000;
    const endsAt = Date.now() + durationMs;
    setForcedExitWindow({ message: nextStatus, endsAt, durationMs });
    setStatus(`${nextStatus} Returning to home in ${seconds}s.`);
    leaveTimerRef.current = window.setTimeout(() => {
      leaveTimerRef.current = null;
      returnToHome(nextStatus);
    }, seconds * 1000);
  }

  function returnToHome(nextStatus) {
    clearLeaveTimer();
    clearFinalStandTimer();
    clearLobbyStartTimer();
    setStatus(nextStatus);
    setHomeView("builder");
    setMatch(null);
    setLobby(null);
    setPhase("");
    setQuestion(null);
    setQuestionWindow(null);
    setPauseWindow(null);
    setPauseMessage("");
    setSelectionPrompt(null);
    setCountdown({ label: "00:00", progress: 0, visible: false });
    setLeaderboard([]);
    setLastResult(null);
    setAnswerFeedback(null);
    setWinnerId(null);
    setHighlights({});
    setMatchConnected(false);
    setMatchConnectionEnabled(false);
    setConnectedPlayerIds([]);
    setForcedExitWindow(null);
    setForcedExitCountdown({ label: "00:00", progress: 0 });
    suppressMatchCloseRef.current = true;
    matchSocketRef.current?.close();
    requestLobbyDirectory();
  }

  function scheduleFinalStandReveal() {
    clearFinalStandTimer();
    const durationMs = 3000;
    const endsAt = Date.now() + durationMs;
    setHomeView("post_match_wait");
    setFinalStandWindow({ endsAt, durationMs });
    finalStandTimerRef.current = window.setTimeout(() => {
      finalStandTimerRef.current = null;
      setFinalStandWindow(null);
      setHomeView("results");
    }, durationMs);
  }

  function hydrateMatchRuntime(mapData, currentState) {
    const hydratedRoundNumber = mapData?.current_round_number || 0;
    setRoundNumber(hydratedRoundNumber);
    if (mapData?.current_question) {
      setQuestion(mapData.current_question);
      setQuestionWindow(mapData.question_window ? {
        startedAt: mapData.question_window.started_at,
        endsAt: mapData.question_window.ends_at,
      } : null);
      setPauseWindow(null);
      setPauseMessage("");
      setSelectionPrompt(null);
      setPhase(currentState || mapData.current_state || "");
      return;
    }
    if (mapData?.pause_window) {
      setPauseWindow(mapData.pause_window ? {
        startedAt: mapData.pause_window.started_at,
        endsAt: mapData.pause_window.ends_at,
      } : null);
      setPauseMessage(mapData.pause_message || "Next question in...");
      setQuestion(null);
      setQuestionWindow(null);
      setSelectionPrompt(null);
      setPhase(currentState || mapData.current_state || "");
      return;
    }
    if (mapData?.current_selection_prompt) {
      setSelectionPrompt({
        activePlayerId: mapData.current_selection_prompt.active_player_id,
        validTargets: mapData.current_selection_prompt.valid_targets || [],
        selectionMode: mapData.current_selection_prompt.selection_mode,
        phase: mapData.current_selection_prompt.phase,
        roundNumber: mapData.current_selection_prompt.round_number,
        message: mapData.current_selection_prompt.message,
      });
      setQuestion(null);
      setQuestionWindow(null);
      setPauseWindow(null);
      setPauseMessage("");
      setPhase(currentState || mapData.current_selection_prompt.phase || "");
      return;
    }
    setQuestion(null);
    setQuestionWindow(null);
    setPauseWindow(null);
    setPauseMessage("");
    setSelectionPrompt(null);
    setPhase(currentState || mapData?.current_state || "");
  }

  useEffect(() => {
    const sounds = {};
    Object.entries(SOUND_PATHS).forEach(([name, path]) => {
      const audio = new Audio(path);
      audio.preload = "auto";
      sounds[name] = audio;
    });
    soundEffectsRef.current = sounds;

    const handleButtonClick = (event) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      const button = target.closest("button");
      if (!button || button.disabled) return;
      playSound("button");
    };

    document.addEventListener("click", handleButtonClick, true);
    return () => {
      document.removeEventListener("click", handleButtonClick, true);
      soundEffectsRef.current = {};
    };
  }, []);

  useEffect(() => {
    const socket = createSocketClient("/ws/matchmaking/", {
      onOpen: () => {
        setConnected(true);
        setStatus("Connected. Choose your name and room.");
      },
      onClose: () => setConnected(false),
      onReconnect: ({ delayMs }) => {
        setConnected(false);
        setStatus(`Reconnecting in ${Math.ceil(delayMs / 1000)}s...`);
      },
      onMessage: (payload) => {
        if (payload.type === "connection.ready") {
          updatePlayer(payload.player);
          nameCheckTargetRef.current = "";
          setNameValidation({ state: "idle", message: "" });
          setOptions(payload.options || { categories: [], question_types: [], difficulties: [], map_sizes: [], battle_round_options: [], game_speed_options: [] });
          setActiveLobbies(payload.active_lobbies || []);
          pushEvent("connection.ready", `Signed in as ${payload.player.display_name}.`);
          return;
        }
        if (payload.type === "profile.reset") {
          updatePlayer(payload.player);
          nameCheckTargetRef.current = "";
          setNameValidation({ state: "idle", message: "" });
          setFormState(INITIAL_FORM_STATE);
          setLobby(null);
          setMatch(null);
          setHomeView("builder");
          setPhase("");
          setQuestion(null);
          setSelectionPrompt(null);
          setRoundNumber(0);
          setLastResult(null);
          setAnswerFeedback(null);
          setLeaderboard([]);
          setActiveLobbies(payload.active_lobbies || []);
          clearLobbyStartTimer();
          clearFinalStandTimer();
          setStatus("Created a fresh guest identity.");
          pushEvent("profile.reset", `Signed in as ${payload.player.display_name}.`);
          requestLobbyDirectory();
          return;
        }
        if (payload.type === "profile.updated") {
          updatePlayer(payload.player);
          if (!displayNameRef.current || displayNameRef.current === payload.player.display_name || nameCheckTargetRef.current === payload.player.display_name) {
            nameCheckTargetRef.current = "";
            setNameValidation({ state: "ready", message: "Name ready" });
          }
          pushEvent("profile.updated", `Name set to ${payload.player.display_name}.`);
          return;
        }
        if (payload.type === "dev.lobbies_cleared") {
          setStatus(`Cleared ${payload.lobby_count} lobbies and ${payload.match_count} active matches.`);
          pushEvent("dev.lobbies_cleared", `Cleared ${payload.lobby_count} lobbies.`);
          return;
        }
        if (payload.type === "lobby.snapshot") {
          setHomeView("builder");
          clearLobbyStartTimer();
          setLobby(payload.lobby);
          setStatus(`Lobby ${payload.lobby.name} ready.`);
          pushEvent("lobby.snapshot", `${payload.lobby.name} updated.`);
          return;
        }
        if (payload.type === "lobby.start_countdown") {
          setLobbyStartWindow({ startedAt: payload.started_at, endsAt: payload.ends_at, message: payload.message || "Starting game in..." });
          playSound("countdown");
          return;
        }
        if (payload.type === "lobby.directory") {
          setActiveLobbies(payload.lobbies || []);
          return;
        }
        if (payload.type === "lobby.closed") {
          setHomeView("builder");
          clearLobbyStartTimer();
          setLobby(null);
          setStatus(payload.message || "Left lobby.");
          pushEvent("lobby.closed", payload.message || "Lobby closed or you left it.");
          requestLobbyDirectory();
          if (payload.reason === "player_left" || payload.reason === "match_finished") {
            return;
          }
          if (payload.reason === "dev_reset") {
            returnToHome(payload.message || "All active lobbies were cleared.");
            return;
          }
          if (!matchRef.current) {
            return;
          }
          if (matchRef.current.currentState !== "finished") {
            setMatch(null);
          }
          return;
        }
        if (payload.type === "match.assigned") {
          clearLobbyStartTimer();
          const nextMatch = normalizeMatch({
            id: payload.match_id,
            current_state: payload.current_state,
            map_data: payload.map_data,
            player_ids: payload.player_ids,
            players: payload.players,
          });
          setMatch(nextMatch);
          setConnectedPlayerIds(nextMatch.connectedPlayerIds || []);
          setPhase(payload.current_state || "queued");
          setRoundNumber(0);
          setQuestion(null);
          setQuestionWindow(null);
          setPauseWindow(null);
          setPauseMessage("");
          setSelectionPrompt(null);
          setLastResult(null);
          setAnswerFeedback(null);
          setLeaderboard([]);
          clearFinalStandTimer();
          setMatchConnectionEnabled(true);
          pushEvent("match.assigned", `Match ${payload.match_id} created.`);
          return;
        }
        if (payload.type === "error") {
          const isDisplayNameError = payload.code === "display_name_in_use" || payload.code === "display_name_invalid";
          if (payload.code === "display_name_in_use") {
            setNameValidation({ state: "error", message: "Name already in use. Choose a different one." });
          } else if (payload.code === "display_name_invalid") {
            setNameValidation({ state: "error", message: payload.message });
          }
          if (!isDisplayNameError) {
            setStatus(payload.message);
          }
          pushEvent("error", payload.message);
        }
      },
    });
    lobbySocketRef.current = socket;
    return () => socket.close();
  }, []);

  useEffect(() => {
    if (!match?.id || !matchConnectionEnabled) return undefined;
    const socket = createSocketClient(`/ws/matches/${match.id}/`, {
      onOpen: () => {
        setMatchConnected(true);
      },
      onClose: () => {
        setMatchConnected(false);
        if (suppressMatchCloseRef.current) {
          suppressMatchCloseRef.current = false;
          return;
        }
        if (match?.currentState !== "finished") {
          returnToHome("Match connection closed.");
        }
      },
      onMessage: (payload) => {
        if (payload.type === "match.ready") {
          updatePlayer(payload.player);
          const hydratedMatch = normalizeMatch(payload.match);
          setMatch(hydratedMatch);
          setConnectedPlayerIds(hydratedMatch.connectedPlayerIds || []);
          setLeaderboard(hydratedMatch.resultView?.leaderboard || hydratedMatch.resultData?.leaderboard || []);
          hydrateMatchRuntime(hydratedMatch.mapData, hydratedMatch.currentState);
          if (hydratedMatch.currentState === "finished") {
            setHomeView("results");
          }
          pushEvent("match.ready", `Entered ${payload.match.lobby_name || "match"}.`);
          return;
        }
        if (payload.type === "player.presence") {
          setConnectedPlayerIds(payload.connected_player_ids || []);
          const actorName = match?.players?.[String(payload.player_id)]?.name || `Player ${payload.player_id}`;
          pushEvent("player.presence", payload.connected ? `${actorName} connected.` : `${actorName} disconnected.`);
          return;
        }
        if (payload.type === "match.state") {
          setMatch((current) => current ? { ...current, currentState: payload.current_state, mapData: payload.map_data || current.mapData } : current);
          hydrateMatchRuntime(payload.map_data || {}, payload.current_state || "");
          return;
        }
        if (payload.type === "selection.prompt") {
          setRoundNumber(payload.round_number || 0);
          setSelectionPrompt({
            activePlayerId: payload.active_player_id,
            validTargets: payload.valid_targets || [],
            selectionMode: payload.selection_mode,
            phase: payload.phase,
            roundNumber: payload.round_number,
            message: payload.message,
          });
          setSelectionSubmittedRound(null);
          setQuestion(null);
          setQuestionWindow(null);
          setPauseWindow(null);
          setPauseMessage("");
          setMatch((current) => current ? { ...current, currentState: payload.phase, mapData: payload.map_data || current.mapData } : current);
          setPhase(payload.phase || "");
          pushEvent("selection.prompt", payload.message);
          return;
        }
        if (payload.type === "selection.locked") {
          if (payload.map_data) {
            setMatch((current) => current ? { ...current, mapData: payload.map_data } : current);
          }
          playSound("selection");
          pushEvent("selection.locked", `Territory ${payload.territory_id} selected.`);
          return;
        }
        if (payload.type === "territory.claimed") {
          setMatch((current) => current ? { ...current, mapData: payload.map_data || current.mapData } : current);
          playSound("territory");
          setHighlights({ [payload.territory_id]: "territory_claimed" });
          if (highlightTimerRef.current) {
            clearTimeout(highlightTimerRef.current);
          }
          highlightTimerRef.current = setTimeout(() => setHighlights({}), 1600);
          const actorName = match?.players?.[String(payload.player_id)]?.name || `Player ${payload.player_id}`;
          pushEvent("territory.claimed", `${actorName} claimed territory ${payload.territory_id}.`);
          return;
        }
        if (payload.type === "round.question") {
          playSound("question");
          setRoundNumber(payload.round_number || 0);
          setQuestion(payload.question || null);
          setPhase(payload.phase || "");
          setSelectionPrompt(null);
          setQuestionWindow({ startedAt: payload.started_at, endsAt: payload.ends_at });
          setPauseWindow(null);
          setPauseMessage("");
          setAnswerSubmittedRound(null);
          setAnswerFeedback(null);
          setForcedExitWindow(null);
          setMatch((current) => current ? { ...current, currentState: payload.phase, mapData: payload.map_data || current.mapData } : current);
          pushEvent("round.question", `${payload.phase.replaceAll("_", " ")} question live.`);
          return;
        }
        if (payload.type === "round.pause") {
          setRoundNumber(payload.round_number || 0);
          setQuestion(null);
          setQuestionWindow(null);
          setSelectionPrompt(null);
          setPauseWindow({ startedAt: payload.started_at, endsAt: payload.ends_at });
          setPauseMessage(payload.message || "Next question in...");
          setMatch((current) => current ? { ...current, currentState: payload.phase, mapData: payload.map_data || current.mapData } : current);
          setPhase(payload.phase || "");
          pushEvent("round.pause", payload.message || "Short pause.");
          return;
        }
        if (payload.type === "round.result") {
          setRoundNumber(payload.round_number || 0);
          setMatch((current) => current ? { ...current, mapData: payload.map_data || current.mapData } : current);
          setQuestion(null);
          setQuestionWindow(null);
          setPauseWindow(null);
          setPauseMessage("");
          setWinnerId(payload.winner_id || null);
          setSelectionPrompt(null);
          const effectMap = Object.fromEntries((payload.effects || []).filter((effect) => effect.territory_id).map((effect) => [effect.territory_id, effect.type]));
          setHighlights(effectMap);
          if (highlightTimerRef.current) {
            clearTimeout(highlightTimerRef.current);
          }
          highlightTimerRef.current = setTimeout(() => {
            setHighlights({});
            setWinnerId(null);
          }, 2200);
          const playerNameLookup = matchRef.current?.players || match?.players || {};
          const isEstimateRound = payload.question_type === "estimate";
          const fastestEntry = Object.entries(payload.answers || {})
            .filter(([, answer]) => Number.isFinite(answer.response_ms))
            .sort((left, right) => left[1].response_ms - right[1].response_ms)[0];
          const fastestLabel = fastestEntry
            ? `${playerNameLookup[String(fastestEntry[0])]?.name || `Player ${fastestEntry[0]}`} in ${(fastestEntry[1].response_ms / 1000).toFixed(2)}s`
            : "No valid response time";
          const closestEntry = Object.entries(payload.answers || {})
            .filter(([, answer]) => Number.isFinite(answer.distance))
            .sort((left, right) => {
              if (left[1].distance !== right[1].distance) {
                return left[1].distance - right[1].distance;
              }
              return (left[1].response_ms ?? Number.POSITIVE_INFINITY) - (right[1].response_ms ?? Number.POSITIVE_INFINITY);
            })[0];
          const closestLabel = closestEntry
            ? `${playerNameLookup[String(closestEntry[0])]?.name || `Player ${closestEntry[0]}`} at ${closestEntry[1].distance} away`
            : "No valid estimate";
          const answerLines = Object.entries(payload.answers || {}).map(([playerId, answer]) => {
            const playerName = playerNameLookup[String(playerId)]?.name || `Player ${playerId}`;
            if (!answer.answer) return `${playerName}: no answer`;
            if (answer.distance != null) {
              const speedNote = Number.isFinite(answer.response_ms) ? `${(answer.response_ms / 1000).toFixed(2)}s` : "no time";
              return `${playerName}: ${answer.answer}, ${answer.distance} away, ${speedNote}`;
            }
            const speedNote = Number.isFinite(answer.response_ms) ? `${(answer.response_ms / 1000).toFixed(2)}s` : "no time";
            return `${playerName}: ${answer.answer} ${answer.is_correct ? "correct" : "wrong"}, ${speedNote}`;
          });
          const localAnswer = payload.answers?.[String(player?.id)];
          if (localAnswer) {
            if (!localAnswer.answer) {
              setAnswerFeedback({
                kind: "neutral",
                title: "No answer submitted",
                body: `Correct answer: ${payload.correct_answer}`,
              });
            } else if (isEstimateRound && payload.winner_id === player?.id && !localAnswer.is_correct) {
              playSound("correct");
              setAnswerFeedback({
                kind: "success",
                title: "Closest answer",
                body: `You answered ${localAnswer.answer}, which was closest to ${payload.correct_answer}.`,
              });
            } else if (isEstimateRound && localAnswer.is_correct && payload.winner_id !== player?.id) {
              setAnswerFeedback({
                kind: "neutral",
                title: "Correct but slower",
                body: `Your estimate was correct, but your opponent answered faster.`,
              });
            } else if (!isEstimateRound && localAnswer.is_correct && payload.winner_id && payload.winner_id !== player?.id) {
              setAnswerFeedback({
                kind: "neutral",
                title: "Correct but slower",
                body: `You answered correctly, but your opponent answered faster.`,
              });
            } else if (localAnswer.is_correct) {
              playSound("correct");
              setAnswerFeedback({
                kind: "success",
                title: "Correct",
                body: `You answered ${localAnswer.answer}.`,
              });
            } else {
              playSound("wrong");
              setAnswerFeedback({
                kind: "error",
                title: "Wrong answer",
                body: `You answered ${localAnswer.answer}. Correct answer: ${payload.correct_answer}`,
              });
            }
          }
          setLastResult({
            title: payload.winner_id ? `Winner: ${playerNameLookup[String(payload.winner_id)]?.name || `Player ${payload.winner_id}`}` : "No round winner",
            body: undefined,
            lines: [
              ...(answerLines.length ? answerLines : ["No valid answers recorded."]),
              isEstimateRound ? `Closest: ${closestLabel}` : `Fastest: ${fastestLabel}`,
            ],
          });
          pushEvent("round.result", `Round ${payload.round_number} resolved.`);
          return;
        }
        if (payload.type === "match.finished") {
          playSound("finish");
          setMatch((current) => current ? {
            ...current,
            currentState: payload.current_state,
            mapData: payload.map_data || current.mapData,
            resultData: payload.result_data || current.resultData || {},
            resultView: payload.result_view || payload.result_data?.result_view || current.resultView || current.resultData?.result_view || null,
          } : current);
          setPhase(payload.current_state || "finished");
          setQuestion(null);
          setQuestionWindow(null);
          setPauseWindow(null);
          setPauseMessage("");
          setSelectionPrompt(null);
          setLeaderboard(payload.result_view?.leaderboard || payload.leaderboard || []);
          setMatchConnected(false);
          if (payload.reason === "player_left") {
            pushEvent("match.finished", payload.message || "A player left the match.");
            scheduleReturnHome(payload.message || "A player left the match.", payload.countdown_seconds);
            return;
          }
          if (payload.reason === "dev_reset") {
            pushEvent("match.finished", payload.message || "All active lobbies were cleared.");
            returnToHome(payload.message || "All active lobbies were cleared.");
            return;
          }
          requestLobbyDirectory();
          scheduleFinalStandReveal();
          pushEvent("match.finished", "Game over.");
          return;
        }
        if (payload.type === "match.left") {
          pushEvent("match.left", "Match leave confirmed.");
          return;
        }
        if (payload.type === "input.submitted") {
          if (payload.action === "submit_answer") setAnswerSubmittedRound(payload.round_number);
          if (payload.action === "select_territory") setSelectionSubmittedRound(payload.round_number);
        }
      },
    }, { autoReconnect: false });
    matchSocketRef.current = socket;
    return () => {
      suppressMatchCloseRef.current = true;
      socket.close();
    };
  }, [match?.id, matchConnectionEnabled, player?.id]);

  useEffect(() => {
    const activeWindow = questionWindow || pauseWindow;
    if (!activeWindow?.startedAt || !activeWindow?.endsAt) {
      setCountdown({ label: "00:00", progress: 0, visible: false });
      return undefined;
    }
    const tick = () => {
      const start = new Date(activeWindow.startedAt).getTime();
      const end = new Date(activeWindow.endsAt).getTime();
      const now = Date.now();
      const remaining = Math.max(0, end - now);
      const total = Math.max(1000, end - start);
      setCountdown({
        label: formatCountdown(Math.ceil(remaining / 1000)),
        progress: remaining / total,
        visible: true,
      });
    };
    tick();
    const id = setInterval(tick, 200);
    return () => clearInterval(id);
  }, [questionWindow, pauseWindow]);

  useEffect(() => {
    if (!forcedExitWindow?.endsAt) {
      setForcedExitCountdown({ label: "00:00", progress: 0 });
      return undefined;
    }

    const tick = () => {
      const remaining = Math.max(0, forcedExitWindow.endsAt - Date.now());
      const total = Math.max(1000, forcedExitWindow.durationMs || 3000);
      setForcedExitCountdown({
        label: formatCountdown(Math.ceil(remaining / 1000)),
        progress: remaining / total,
      });
    };

    tick();
    const id = window.setInterval(tick, 100);
    return () => window.clearInterval(id);
  }, [forcedExitWindow]);

  useEffect(() => {
    if (!lobbyStartWindow?.startedAt || !lobbyStartWindow?.endsAt) {
      setLobbyStartCountdown({ label: "00:00", progress: 0 });
      return undefined;
    }
    const tick = () => {
      const start = new Date(lobbyStartWindow.startedAt).getTime();
      const end = new Date(lobbyStartWindow.endsAt).getTime();
      const now = Date.now();
      const remaining = Math.max(0, end - now);
      const total = Math.max(1000, end - start);
      setLobbyStartCountdown({
        label: formatCountdown(Math.ceil(remaining / 1000)),
        progress: remaining / total,
      });
    };
    tick();
    const id = window.setInterval(tick, 100);
    return () => window.clearInterval(id);
  }, [lobbyStartWindow]);

  useEffect(() => () => {
    if (highlightTimerRef.current) clearTimeout(highlightTimerRef.current);
    clearLeaveTimer();
    clearFinalStandTimer();
    clearLobbyStartTimer();
  }, []);

  useEffect(() => {
    if (!finalStandWindow?.endsAt) {
      setFinalStandCountdown({ label: "00:00", progress: 0 });
      return undefined;
    }

    const tick = () => {
      const remaining = Math.max(0, finalStandWindow.endsAt - Date.now());
      const total = Math.max(1000, finalStandWindow.durationMs || 3000);
      setFinalStandCountdown({
        label: formatCountdown(Math.ceil(remaining / 1000)),
        progress: remaining / total,
      });
    };

    tick();
    const id = window.setInterval(tick, 100);
    return () => window.clearInterval(id);
  }, [finalStandWindow]);

  function maybeSendProfile() {
    const desiredName = formState.displayName.trim();
    if (desiredName.length < 2) {
      setStatus("Choose a visible name before joining or creating a lobby.");
      return false;
    }
    if (player?.display_name === desiredName) {
      return true;
    }
    if (nameValidation.state === "checking") {
      setStatus("Wait for the name check to finish.");
      return false;
    }
    if (nameValidation.state === "error" && nameCheckTargetRef.current === desiredName) {
      return false;
    }
    if (nameCheckTargetRef.current !== desiredName || nameValidation.state === "idle") {
      nameCheckTargetRef.current = desiredName;
      setNameValidation({ state: "checking", message: "Checking name..." });
      sendLobby({ action: "set_profile", display_name: desiredName });
    }
    setStatus("Finish choosing an available name first.");
    return false;
  }

  function createLobby() {
    if (!maybeSendProfile()) return;
    sendLobby({
      action: "create_lobby",
      lobby_name: formState.createLobbyName.trim(),
      categories: formState.categories,
      question_types: formState.questionTypes,
      difficulties: formState.difficulties,
      test_mode: false,
      timer_seconds: formState.gameSpeed,
      map_radius: formState.mapRadius,
      battle_rounds: formState.battleRounds,
    });
  }

  function joinLobby(lobbyNameOverride) {
    if (!maybeSendProfile()) return;
    const lobbyName = typeof lobbyNameOverride === "string" ? lobbyNameOverride.trim() : formState.joinLobbyName.trim();
    setFormState((current) => ({ ...current, joinLobbyName: lobbyName }));
    sendLobby({ action: "join_lobby", lobby_name: lobbyName });
  }

  function leaveLobby() {
    sendLobby({ action: "leave_lobby" });
  }

  function startGame() {
    sendLobby({ action: "start_game" });
  }

  function resetIdentity() {
    if (!window.confirm("Reset your identity, clear the current lobby state, and get a fresh guest id?")) {
      return;
    }
    clearLobbyStartTimer();
    clearFinalStandTimer();
    window.sessionStorage.removeItem("triviadorGuestToken");
    sendLobby({ action: "reset_identity" });
  }

  function disconnectMatch() {
    if (!window.confirm("Are you sure?")) {
      return;
    }
    if (matchSocketRef.current?.readyState === WebSocket.OPEN) {
      matchSocketRef.current.send(JSON.stringify({ action: "leave_match" }));
      setStatus("Leaving match...");
      return;
    }
    returnToHome("You left the match.");
  }

  function submitAnswer(answer) {
    if (!matchSocketRef.current || matchSocketRef.current.readyState !== WebSocket.OPEN) return;
    matchSocketRef.current.send(JSON.stringify({ action: "submit_answer", round_number: roundNumber || 1, answer }));
  }

  function selectTerritory(territoryId) {
    if (!selectionPrompt || !matchSocketRef.current || matchSocketRef.current.readyState !== WebSocket.OPEN) return;
    matchSocketRef.current.send(JSON.stringify({ action: "select_territory", round_number: selectionPrompt.roundNumber, territory_id: territoryId }));
  }

  const cells = match?.mapData?.cells || [];
  const playerLookup = match?.players || {};
  const localPlayerId = player?.id;
  const opponent = match?.playerIds?.find((playerId) => playerId !== localPlayerId);
  const opponentConnected = opponent != null ? connectedPlayerIds.includes(opponent) : true;
  const counts = cells.reduce((accumulator, cell) => {
    if (cell.owner_id != null) {
      accumulator[cell.owner_id] = (accumulator[cell.owner_id] || 0) + 1;
    }
    return accumulator;
  }, {});
  const localBase = cells.find((cell) => cell.is_base && cell.base_owner_id === localPlayerId);
  const opponentBase = cells.find((cell) => cell.is_base && cell.base_owner_id === opponent);
  const canSelect = Boolean(matchConnected && selectionPrompt && selectionPrompt.activePlayerId === localPlayerId && selectionSubmittedRound !== selectionPrompt.roundNumber);
  const answerDisabled = !matchConnected || !question || answerSubmittedRound != null || match?.currentState === "finished";
  const currentBattleState = phase || match?.currentState || "queued";
  const currentBattleRound = ["duel", "base_siege"].includes(currentBattleState)
    ? Math.min((match?.mapData?.battle_rounds_played || 0) + 1, match?.mapData?.round_cap || match?.settings?.battle_rounds || 12)
    : null;
  const battleRoundCap = match?.mapData?.round_cap || match?.settings?.battle_rounds || 12;
  const resultView = match?.resultView || match?.resultData?.result_view || null;
  const holdingOnBoardAfterFinish = match?.currentState === "finished" && homeView === "post_match_wait";
  const attackedTerritoryId = match?.mapData?.current_context?.target_territory_id ?? null;
  const panelCountdown = holdingOnBoardAfterFinish
    ? { label: finalStandCountdown.label, progress: finalStandCountdown.progress, visible: true }
    : countdown;
  const panelPauseMessage = holdingOnBoardAfterFinish ? "Opening final results..." : pauseMessage;

  return (
    <div className="app-shell">
      <div className="ambient ambient-one" />
      <div className="ambient ambient-two" />
      <header className="topbar">
        <div>
          <span className="brand-mark">OTA</span>
          <div className="brand-copy">
            <strong>OTA: Open Trivia Arena</strong>
          </div>
        </div>
        <div className="topbar-status">
          <span className="status-label">id:</span>
          <span>{player?.display_name || "Guest"}</span>
        </div>
      </header>

      {!match || (match.currentState === "finished" && homeView === "results") ? (
        <main className="lobby-main">
          {match?.currentState === "finished" && homeView === "results" ? (
            <FinalStandPage
              leaderboard={leaderboard}
              resultView={resultView}
              playerLookup={playerLookup}
              title={`${match.lobbyName || lobby?.name || "Match"} results`}
              onReturnHome={() => returnToHome("Returned to home.")}
            />
          ) : lobby ? (
            <LobbyRoom lobby={lobby} player={player} onStart={startGame} onLeave={leaveLobby} startCountdownLabel={lobbyStartWindow ? lobbyStartCountdown.label : ""} />
          ) : homeView === "directory" ? (
            <LobbyDirectoryPage lobbies={activeLobbies} onJoin={joinLobby} onBack={() => setHomeView("builder")} />
          ) : (
            <IdentityAndLobbyForm
              formState={formState}
              setFormState={setFormState}
              options={options}
              onCreateLobby={createLobby}
              onJoinLobby={joinLobby}
              onDisplayNameChange={handleDisplayNameChange}
              nameValidation={nameValidation}
              onResetIdentity={resetIdentity}
              onShowActiveLobbies={() => {
                requestLobbyDirectory();
                setHomeView("directory");
              }}
              status={status}
              connected={connected}
            />
          )}
        </main>
      ) : (
        <main className="game-main">
          <section className="board-column glass-panel">
            <div className="game-header-band">
              <PlayerPanel
                title="YOU"
                playerName={playerLookup[String(localPlayerId)]?.name || player?.display_name}
                territories={counts[localPlayerId] || 0}
                baseHp={localBase?.hp || 0}
                active={selectionPrompt?.activePlayerId === localPlayerId}
              />
              <div className="phase-center">
                <span className="eyebrow">{match.lobbyName || lobby?.name || "Match"}</span>
                <strong>{(phase || match.currentState || "queued").replaceAll("_", " ")}</strong>
                <span>radius {match.mapData?.map_radius || match.settings?.map_radius || 3}</span>
                {currentBattleRound != null && <span>battle round {currentBattleRound} / {battleRoundCap}</span>}
              </div>
              <PlayerPanel
                title="OPPONENT"
                playerName={playerLookup[String(opponent)]?.name || "Waiting"}
                territories={counts[opponent] || 0}
                baseHp={opponentBase?.hp || 0}
                active={selectionPrompt?.activePlayerId === opponent}
              />
            </div>

            <SelectionBanner selectionPrompt={selectionPrompt} localPlayerId={localPlayerId} playerLookup={playerLookup} />

            <HexBoard
              cells={cells}
              localPlayerId={localPlayerId}
              validTargets={selectionPrompt?.validTargets || match.mapData?.valid_targets || []}
              onSelect={selectTerritory}
              selectionEnabled={canSelect}
              winnerId={winnerId}
              highlights={highlights}
              attackedTerritoryId={attackedTerritoryId}
            />
          </section>

          <section className="side-column">
            <div className="glass-panel side-actions">
              {forcedExitWindow && (
                <div className="disconnect-warning">
                  <span className="eyebrow">Disconnect Notice</span>
                  <strong>{forcedExitWindow.message}</strong>
                  <div className="disconnect-warning-time">Returning home in {forcedExitCountdown.label}</div>
                  <div className="disconnect-warning-track">
                    <div className="disconnect-warning-fill" style={{ width: `${forcedExitCountdown.progress * 100}%` }} />
                  </div>
                </div>
              )}
              <div className={`presence-banner ${matchConnected ? "presence-ok" : "presence-offline"}`}>
                <strong>{matchConnected ? "Game connected" : "Game disconnected"}</strong>
                <span>{opponentConnected ? `${playerLookup[String(opponent)]?.name || "Opponent"} is connected.` : `${playerLookup[String(opponent)]?.name || "Opponent"} disconnected.`}</span>
              </div>
              <button type="button" className="danger-btn" onClick={disconnectMatch}>
                Leave Match
              </button>
            </div>
            <QuestionPanel
              question={question}
              phase={phase || match.currentState || "queued"}
              countdown={panelCountdown}
              showCountdown={panelCountdown.visible}
              onAnswer={submitAnswer}
              lastResult={lastResult}
              answerFeedback={answerFeedback}
              pauseMessage={panelPauseMessage}
              disabled={answerDisabled}
            />
          </section>
        </main>
      )}
    </div>
  );
}
