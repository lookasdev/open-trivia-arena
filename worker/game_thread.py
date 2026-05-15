from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import timedelta
from queue import Empty, Queue

import django
from django.utils import timezone
from django.utils.dateparse import parse_datetime


django.setup()

from apps.matches.models import LobbyState, Match, MatchState  # noqa: E402
from apps.trivia.models import Question, QuestionType  # noqa: E402
from worker.hex_utils import build_adjacency, build_hex_cells, cells_by_id, find_player_base, owned_neighbor_targets, territory_counts  # noqa: E402
from worker.mq import publish_game_event  # noqa: E402
from worker.question_provider import ensure_test_mode_questions, fetch_runtime_questions  # noqa: E402


@dataclass
class SubmittedInput:
    player_id: int
    round_number: int
    action: str
    value: str
    received_at: str


class GameThread(threading.Thread):
    def __init__(self, match_id: str, player_ids: list[int], average_elo: int, settings_data: dict | None = None, timer_seconds: int = 15, pause_seconds: int = 3):
        super().__init__(daemon=True)
        self.match_id = match_id
        self.player_ids = player_ids
        self.average_elo = average_elo
        self.settings_data = settings_data or {}
        self.timer_seconds = timer_seconds
        self.pause_seconds = pause_seconds
        self.test_mode = bool(self.settings_data.get("test_mode"))
        self.input_queue: Queue[SubmittedInput] = Queue()
        self._stop_event = threading.Event()
        self._used_question_ids: set[int] = set()
        self.round_number = 0
        self.map_data = self._load_map_data()
        self.cells = self.map_data["cells"]
        self.adjacency = build_adjacency(self.cells)
        self.siege_state = {"territory_id": None, "attacker_id": None, "streak": 0}
        self.remote_batch_index = 0
        self.remote_question_queue: list[Question] = []
        self.test_questions = ensure_test_mode_questions() if self.test_mode else []
        self.test_question_index = 0

    def submit_input(self, game_input: SubmittedInput) -> None:
        self.input_queue.put(game_input)

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        self._ensure_stats()
        pick_order = self._run_base_selection()
        if self._stop_event.is_set():
            return
        if self._is_game_over():
            self._finish_match(reason="base_selection_complete")
            return

        self._run_expansion(pick_order)
        if self._stop_event.is_set():
            return
        if self._is_game_over():
            self._finish_match(reason="expansion_complete")
            return

        self._run_battles(pick_order)
        if self._stop_event.is_set():
            return
        if self._is_game_over():
            self._finish_match(reason="battle_victory")
            return

        self._finish_match(reason="battle_round_limit")

    def _load_map_data(self) -> dict:
        match = Match.objects.get(public_id=self.match_id)
        if match.map_data and match.map_data.get("cells"):
            return match.map_data

        radius = int(self.settings_data.get("map_radius") or 3)
        cells = build_hex_cells(radius)
        stats = {
            str(player_id): {
                "correct_answers": 0,
                "won_questions": 0,
                "territories_won": 0,
                "answers_submitted": 0,
                "total_response_ms": 0,
            }
            for player_id in self.player_ids
        }
        return {
            "map_radius": radius,
            "cells": cells,
            "stats": stats,
            "active_player_id": None,
            "valid_targets": [],
            "selection_mode": None,
            "current_selection_prompt": None,
            "current_question": None,
            "question_window": None,
            "pause_window": None,
            "pause_message": None,
            "current_context": None,
            "current_round_number": 0,
            "pick_order": [],
            "battle_rounds_played": 0,
            "round_cap": int(self.settings_data.get("battle_rounds") or 12),
            "leaderboard": [],
        }

    def _ensure_stats(self) -> None:
        stats = self.map_data.setdefault("stats", {})
        for player_id in self.player_ids:
            stats.setdefault(
                str(player_id),
                {
                    "correct_answers": 0,
                    "won_questions": 0,
                    "territories_won": 0,
                    "answers_submitted": 0,
                    "total_response_ms": 0,
                },
            )
            stats[str(player_id)].setdefault("won_questions", 0)
        self.map_data["round_cap"] = int(self.settings_data.get("battle_rounds") or self.map_data.get("round_cap") or 12)
        self.map_data.setdefault("battle_rounds_played", 0)
        self._persist_map_data()

    def _persist_map_data(self, current_state: str | None = None, result_data: dict | None = None) -> None:
        fields = ["map_data", "updated_at"]
        match = Match.objects.get(public_id=self.match_id)
        match.map_data = self.map_data
        if current_state is not None:
            match.current_state = current_state
            fields.append("current_state")
        if result_data is not None:
            match.result_data = result_data
            fields.append("result_data")
        match.save(update_fields=fields)

    def _publish(self, payload: dict, routing_key: str = "match.event") -> None:
        publish_game_event(payload, routing_key=routing_key)

    def _set_match_state(self, state: str, *, active_player_id: int | None = None, valid_targets: list[int] | None = None, selection_mode: str | None = None) -> None:
        self.map_data["active_player_id"] = active_player_id
        self.map_data["valid_targets"] = valid_targets or []
        self.map_data["selection_mode"] = selection_mode
        self.map_data["current_state"] = state
        self.map_data["pause_window"] = None
        self.map_data["pause_message"] = None
        self._persist_map_data(current_state=state)
        self._publish(
            {
                "type": "match.state",
                "match_id": self.match_id,
                "current_state": state,
                "map_data": self.map_data,
                "player_ids": self.player_ids,
            }
        )

    def _run_base_selection(self) -> list[int]:
        self._set_match_state(MatchState.BASE_SELECTION)
        self.round_number += 1
        question = self._next_question()
        question_result = self._run_question_round(
            phase=MatchState.BASE_SELECTION,
            question=question,
            participants=self.player_ids,
            context={"step": "priority_challenge"},
        )
        pick_order = self._pick_order_from_answers(question, question_result["answers"], self.player_ids)
        self._publish_round_result(result=question_result, effects=[])
        self._run_result_hold_if_needed(question_result, self.player_ids)
        self.map_data["pick_order"] = pick_order
        self._persist_map_data(current_state=MatchState.BASE_SELECTION)
        self._publish(
            {
                "type": "base.pick_order",
                "match_id": self.match_id,
                "round_number": self.round_number,
                "phase": MatchState.BASE_SELECTION,
                "pick_order": pick_order,
                "map_data": self.map_data,
                "player_ids": self.player_ids,
            }
        )

        for player_id in pick_order:
            valid_targets = [cell["territory_id"] for cell in self.cells if cell["owner_id"] is None]
            territory_id = self._prompt_for_selection(
                phase=MatchState.BASE_SELECTION,
                selection_mode="base_pick",
                player_id=player_id,
                valid_targets=valid_targets,
                message="Choose your home base.",
            )
            if territory_id is None:
                continue
            cell = cells_by_id(self.cells)[territory_id]
            cell["owner_id"] = player_id
            cell["is_base"] = True
            cell["base_owner_id"] = player_id
            cell["hp"] = 3
            self._persist_map_data(current_state=MatchState.BASE_SELECTION)
            self._publish(
                {
                    "type": "base.selected",
                    "match_id": self.match_id,
                    "round_number": self.round_number,
                    "phase": MatchState.BASE_SELECTION,
                    "player_id": player_id,
                    "territory_id": territory_id,
                    "map_data": self.map_data,
                    "player_ids": self.player_ids,
                }
            )
        return pick_order

    def _run_expansion(self, pick_order: list[int]) -> None:
        priority_order = pick_order or self.player_ids
        while self._neutral_cells_exist() and not self._stop_event.is_set():
            self._set_match_state(MatchState.EXPANSION)
            self.round_number += 1
            question = self._next_question()
            result = self._run_question_round(
                phase=MatchState.EXPANSION,
                question=question,
                participants=self.player_ids,
                context={"step": "expansion_race"},
            )
            winner_id = None
            loser_id = None
            if question.question_type == QuestionType.ESTIMATE or any(
                result["answers"].get(str(player_id), {}).get("is_correct") for player_id in self.player_ids
            ):
                winner_id, loser_id = self._resolve_expansion_priority(question, result["answers"], priority_order)
            result["winner_id"] = winner_id
            self._publish_round_result(result=result, effects=[])
            self._run_result_hold_if_needed(result, self.player_ids)
            self._run_expansion_claim_sequence(winner_id, loser_id)
            if self._dominant_owner_id() is not None and not self._neutral_cells_exist():
                break
            if self._neutral_cells_exist() and not self._stop_event.is_set():
                self._run_pause(MatchState.EXPANSION, "Next expansion question in...")

    def _run_battles(self, pick_order: list[int]) -> None:
        turn_order = pick_order or self.player_ids
        stagnation_rounds = 0
        while not self._stop_event.is_set() and not self._is_game_over():
            if self.map_data["battle_rounds_played"] >= self.map_data.get("round_cap", 12):
                break
            forced_siege = self._active_siege_target()
            if forced_siege is not None:
                attacker_id = forced_siege["attacker_id"]
                territory_id = forced_siege["territory_id"]
                valid_targets = [territory_id]
                phase = MatchState.BASE_SIEGE
            else:
                attacker_id = turn_order[self.map_data["battle_rounds_played"] % len(turn_order)]
                valid_targets = self._valid_targets_for_player(attacker_id, mode="battle")
                if not valid_targets:
                    stagnation_rounds += 1
                    self.map_data["battle_rounds_played"] += 1
                    if stagnation_rounds >= len(turn_order):
                        break
                    continue
                stagnation_rounds = 0
                territory_id = self._prompt_for_selection(
                    phase=MatchState.DUEL,
                    selection_mode="attack",
                    player_id=attacker_id,
                    valid_targets=valid_targets,
                    message="Choose an adjacent enemy hex to attack.",
                )
                if territory_id is None:
                    break

                target = cells_by_id(self.cells)[territory_id]
                defender_id = target["owner_id"]
                if defender_id is None:
                    self.map_data["battle_rounds_played"] += 1
                    continue
                phase = MatchState.BASE_SIEGE if target["is_base"] and target["base_owner_id"] == defender_id else MatchState.DUEL

            target = cells_by_id(self.cells)[territory_id]
            defender_id = target["owner_id"]
            if defender_id is None:
                self.map_data["battle_rounds_played"] += 1
                self.siege_state = {"territory_id": None, "attacker_id": None, "streak": 0}
                continue
            self._set_match_state(phase, active_player_id=attacker_id, valid_targets=valid_targets, selection_mode="attack")
            self.round_number += 1
            question = self._next_question()
            result = self._run_question_round(
                phase=phase,
                question=question,
                participants=[attacker_id, defender_id],
                context={
                    "target_territory_id": territory_id,
                    "attacker_id": attacker_id,
                    "defender_id": defender_id,
                },
            )
            effects = self._resolve_attack(
                phase=phase,
                territory_id=territory_id,
                attacker_id=attacker_id,
                defender_id=defender_id,
                result=result,
            )
            if phase != MatchState.BASE_SIEGE or not self._active_siege_target():
                self.map_data["battle_rounds_played"] += 1
            self._publish_round_result(result=result, effects=effects)
            self._run_result_hold_if_needed(result, [attacker_id, defender_id])
            if self._is_game_over():
                break

    def _active_siege_target(self) -> dict | None:
        territory_id = self.siege_state.get("territory_id")
        attacker_id = self.siege_state.get("attacker_id")
        streak = self.siege_state.get("streak", 0)
        if territory_id is None or attacker_id is None or streak <= 0:
            return None

        target = cells_by_id(self.cells).get(territory_id)
        if target is None or not target["is_base"] or target["owner_id"] in {None, attacker_id}:
            self.siege_state = {"territory_id": None, "attacker_id": None, "streak": 0}
            return None

        if territory_id not in self._valid_targets_for_player(attacker_id, mode="battle"):
            self.siege_state = {"territory_id": None, "attacker_id": None, "streak": 0}
            return None

        return {"territory_id": territory_id, "attacker_id": attacker_id}

    def _run_question_round(self, phase: str, question: Question, participants: list[int], context: dict) -> dict:
        started_at = timezone.now()
        ends_at = None if self.test_mode else started_at + timedelta(seconds=self.timer_seconds)
        self.map_data["current_round_number"] = self.round_number
        self.map_data["current_context"] = context
        self.map_data["current_selection_prompt"] = None
        self.map_data["pause_window"] = None
        self.map_data["pause_message"] = None
        self.map_data["current_question"] = {
            "id": question.id,
            "text": question.text,
            "options": question.options,
            "question_type": question.question_type,
            "difficulty": question.source_difficulty or question.difficulty_score,
            "category": question.category,
        }
        self.map_data["question_window"] = None if self.test_mode else {
            "started_at": started_at.isoformat(),
            "ends_at": ends_at.isoformat(),
            "timer_seconds": self.timer_seconds,
        }
        self._persist_map_data(current_state=phase)
        self._publish(
            {
                "type": "round.question",
                "match_id": self.match_id,
                "round_number": self.round_number,
                "phase": phase,
                "timer_seconds": None if self.test_mode else self.timer_seconds,
                "started_at": None if self.test_mode else started_at.isoformat(),
                "ends_at": None if self.test_mode else ends_at.isoformat(),
                "question": {
                    "id": question.id,
                    "text": question.text,
                    "options": question.options,
                    "question_type": question.question_type,
                    "difficulty": question.source_difficulty or question.difficulty_score,
                    "category": question.category,
                },
                "context": context,
                "map_data": self.map_data,
                "player_ids": self.player_ids,
            }
        )
        answers = self._collect_inputs(round_number=self.round_number, action="submit_answer", expected_players=participants)
        evaluations = self._evaluate_answers(question, answers, started_at, participants)
        winner_id = self._determine_round_winner(question, evaluations, participants)
        self._record_question_win(winner_id)
        return {
            "phase": phase,
            "question": question,
            "winner_id": winner_id,
            "answers": evaluations,
            "context": context,
        }

    def _publish_round_result(self, result: dict, effects: list[dict]) -> None:
        self.map_data["current_question"] = None
        self.map_data["question_window"] = None
        self.map_data["current_selection_prompt"] = None
        self.map_data["pause_window"] = None
        self.map_data["pause_message"] = None
        self._persist_map_data(current_state=result["phase"])
        self._publish(
            {
                "type": "round.result",
                "match_id": self.match_id,
                "round_number": self.round_number,
                "phase": result["phase"],
                "question_type": result["question"].question_type,
                "winner_id": result["winner_id"],
                "correct_answer": result["question"].correct_answer,
                "resolved_at": timezone.now().isoformat(),
                "answers": result["answers"],
                "effects": effects,
                "map_data": self.map_data,
                "player_ids": self.player_ids,
            }
        )

    def _prompt_for_selection(self, phase: str, selection_mode: str, player_id: int, valid_targets: list[int], message: str) -> int | None:
        self._set_match_state(phase, active_player_id=player_id, valid_targets=valid_targets, selection_mode=selection_mode)
        self.map_data["current_round_number"] = self.round_number
        self.map_data["current_context"] = None
        self.map_data["current_question"] = None
        self.map_data["question_window"] = None
        self.map_data["pause_window"] = None
        self.map_data["pause_message"] = None
        self.map_data["current_selection_prompt"] = {
            "active_player_id": player_id,
            "valid_targets": valid_targets,
            "selection_mode": selection_mode,
            "phase": phase,
            "round_number": self.round_number,
            "message": message,
        }
        self._persist_map_data(current_state=phase)
        self._publish(
            {
                "type": "selection.prompt",
                "match_id": self.match_id,
                "round_number": self.round_number,
                "phase": phase,
                "selection_mode": selection_mode,
                "active_player_id": player_id,
                "valid_targets": valid_targets,
                "message": message,
                "map_data": self.map_data,
                "player_ids": self.player_ids,
            }
        )
        selections = self._collect_inputs(round_number=self.round_number, action="select_territory", expected_players=[player_id])
        if not selections:
            return valid_targets[0] if valid_targets else None
        raw_value = selections[player_id].value
        try:
            territory_id = int(raw_value)
        except (TypeError, ValueError):
            territory_id = valid_targets[0] if valid_targets else None
        if territory_id not in valid_targets:
            territory_id = valid_targets[0] if valid_targets else None
        if territory_id is not None:
            self._publish(
                {
                    "type": "selection.locked",
                    "match_id": self.match_id,
                    "round_number": self.round_number,
                    "phase": phase,
                    "selection_mode": selection_mode,
                    "player_id": player_id,
                    "territory_id": territory_id,
                    "map_data": self.map_data,
                    "player_ids": self.player_ids,
                }
            )
        return territory_id

    def _run_pause(self, phase: str, message: str, *, force: bool = False) -> None:
        if self.test_mode and not force:
            return
        started_at = timezone.now()
        ends_at = started_at + timedelta(seconds=self.pause_seconds)
        self.map_data["current_question"] = None
        self.map_data["question_window"] = None
        self.map_data["current_selection_prompt"] = None
        self.map_data["pause_window"] = {
            "started_at": started_at.isoformat(),
            "ends_at": ends_at.isoformat(),
            "timer_seconds": self.pause_seconds,
        }
        self.map_data["pause_message"] = message
        self._persist_map_data(current_state=phase)
        self._publish(
            {
                "type": "round.pause",
                "match_id": self.match_id,
                "round_number": self.round_number,
                "phase": phase,
                "message": message,
                "timer_seconds": self.pause_seconds,
                "started_at": started_at.isoformat(),
                "ends_at": ends_at.isoformat(),
                "map_data": self.map_data,
                "player_ids": self.player_ids,
            }
        )
        deadline = time.time() + self.pause_seconds
        while time.time() < deadline and not self._stop_event.is_set():
            time.sleep(0.1)

    def _run_result_hold_if_needed(self, result: dict, participants: list[int]) -> None:
        if len(participants) < 2:
            return
        message = "Next step in..."
        if result["phase"] == MatchState.EXPANSION:
            message = "Territory selection in..."
        if result["phase"] == MatchState.BASE_SIEGE:
            message = "Siege continues in..."
        self._run_pause(result["phase"], message, force=True)

    def _resolve_expansion_priority(self, question: Question, evaluations: dict[str, dict], priority_order: list[int]) -> tuple[int | None, int | None]:
        ranking: list[tuple[float, int, int, int]] = []
        for player_id in self.player_ids:
            entry = evaluations[str(player_id)]
            response_ms = entry["response_ms"] if entry["response_ms"] is not None else 10**9
            priority_index = priority_order.index(player_id) if player_id in priority_order else len(priority_order)
            if question.question_type == QuestionType.ESTIMATE:
                distance = entry["distance"] if entry["distance"] is not None else float("inf")
                ranking.append((distance, response_ms, priority_index, player_id))
                continue
            ranking.append((0 if entry["is_correct"] else 1, response_ms, priority_index, player_id))
        ranking.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
        winner_id = ranking[0][3] if ranking else None
        loser_id = ranking[1][3] if len(ranking) > 1 else None
        return winner_id, loser_id

    def _run_expansion_claim_sequence(self, winner_id: int | None, loser_id: int | None) -> None:
        claim_plan = []
        if winner_id is not None:
            claim_plan.append((winner_id, 2))
        if loser_id is not None:
            claim_plan.append((loser_id, 1))

        claimed_this_round: set[int] = set()
        for player_id, claim_count in claim_plan:
            for claim_index in range(claim_count):
                valid_targets = self._expansion_targets_for_claim(player_id, claimed_this_round)
                if not valid_targets:
                    break
                territory_id = self._prompt_for_selection(
                    phase=MatchState.EXPANSION,
                    selection_mode="expand_bonus" if claim_count > 1 else "expand",
                    player_id=player_id,
                    valid_targets=valid_targets,
                    message=self._expansion_claim_message(claim_count, claim_index),
                )
                if territory_id is None:
                    break
                self._claim_territory(player_id, territory_id, MatchState.EXPANSION)
                claimed_this_round.add(territory_id)

    def _expansion_targets_for_claim(self, player_id: int, claimed_this_round: set[int]) -> list[int]:
        return [
            territory_id
            for territory_id in self._valid_targets_for_player(player_id, mode="expansion")
            if territory_id not in claimed_this_round
        ]

    @staticmethod
    def _expansion_claim_message(claim_count: int, claim_index: int) -> str:
        if claim_count == 1:
            return "Choose 1 territory."
        return f"You won the race. Choose territory {claim_index + 1} of {claim_count}."

    def _claim_territory(self, player_id: int, territory_id: int, phase: str) -> None:
        cell = cells_by_id(self.cells)[territory_id]
        if cell["owner_id"] is not None:
            return
        cell["owner_id"] = player_id
        cell["hp"] = 1
        self.map_data["stats"][str(player_id)]["territories_won"] += 1
        self.map_data["current_selection_prompt"] = None
        self._persist_map_data(current_state=phase)
        self._publish(
            {
                "type": "territory.claimed",
                "match_id": self.match_id,
                "round_number": self.round_number,
                "phase": phase,
                "player_id": player_id,
                "territory_id": territory_id,
                "map_data": self.map_data,
                "player_ids": self.player_ids,
            }
        )

    def _collect_inputs(self, round_number: int, action: str, expected_players: list[int]) -> dict[int, SubmittedInput]:
        expected = set(expected_players)
        results: dict[int, SubmittedInput] = {}
        deadline = None if self.test_mode else time.time() + self.timer_seconds
        while len(results) < len(expected) and not self._stop_event.is_set():
            if deadline is not None and time.time() >= deadline:
                break
            try:
                item = self.input_queue.get(timeout=0.25)
            except Empty:
                continue
            if item.round_number != round_number or item.action != action:
                continue
            if item.player_id not in expected or item.player_id in results:
                continue
            results[item.player_id] = item
        return results

    def _evaluate_answers(self, question: Question, answers: dict[int, SubmittedInput], started_at, participants: list[int]) -> dict[str, dict]:
        evaluations: dict[str, dict] = {}
        for player_id in participants:
            answer = answers.get(player_id)
            response_ms = None
            answer_value = ""
            is_correct = False
            distance = None
            if answer is not None:
                answer_value = answer.value
                response_ms = self._response_ms(started_at, answer.received_at)
                player_stats = self.map_data["stats"][str(player_id)]
                player_stats["answers_submitted"] += 1
                player_stats["total_response_ms"] += response_ms
                if question.question_type == QuestionType.ESTIMATE:
                    guess = self._parse_float(answer.value)
                    target = self._parse_float(question.numeric_answer)
                    if guess is not None and target is not None:
                        distance = round(abs(guess - target), 2)
                        is_correct = distance == 0
                else:
                    is_correct = answer.value.strip().lower() == question.correct_answer.strip().lower()
                if is_correct:
                    player_stats["correct_answers"] += 1
            evaluations[str(player_id)] = {
                "answer": answer_value,
                "received_at": answer.received_at if answer is not None else None,
                "response_ms": response_ms,
                "is_correct": is_correct,
                "distance": distance,
            }
        return evaluations

    def _determine_round_winner(self, question: Question, evaluations: dict[str, dict], participants: list[int]) -> int | None:
        if question.question_type == QuestionType.ESTIMATE:
            ranked: list[tuple[float, int, int]] = []
            for player_id in participants:
                entry = evaluations[str(player_id)]
                if entry["distance"] is None or entry["response_ms"] is None:
                    continue
                ranked.append((entry["distance"], entry["response_ms"], player_id))
            if not ranked:
                return None
            ranked.sort(key=lambda item: (item[0], item[1], item[2]))
            return ranked[0][2]

        correct = []
        for player_id in participants:
            entry = evaluations[str(player_id)]
            if entry["is_correct"] and entry["response_ms"] is not None:
                correct.append((entry["response_ms"], player_id))
        if correct:
            correct.sort(key=lambda item: (item[0], item[1]))
            return correct[0][1]
        return None

    def _record_question_win(self, winner_id: int | None) -> None:
        if winner_id is None:
            return
        player_stats = self.map_data["stats"].setdefault(str(winner_id), {})
        player_stats["won_questions"] = int(player_stats.get("won_questions") or 0) + 1

    def _pick_order_from_answers(self, question: Question, evaluations: dict[str, dict], participants: list[int]) -> list[int]:
        if question.question_type == QuestionType.ESTIMATE:
            ranked = []
            for player_id in participants:
                entry = evaluations[str(player_id)]
                distance = entry["distance"] if entry["distance"] is not None else float("inf")
                response_ms = entry["response_ms"] if entry["response_ms"] is not None else 10**9
                ranked.append((distance, response_ms, player_id))
            ranked.sort(key=lambda item: (item[0], item[1], item[2]))
            return [item[2] for item in ranked]

        ranked = []
        for player_id in participants:
            entry = evaluations[str(player_id)]
            response_ms = entry["response_ms"] if entry["response_ms"] is not None else 10**9
            ranked.append((0 if entry["is_correct"] else 1, response_ms, player_id))
        ranked.sort(key=lambda item: (item[0], item[1], item[2]))
        return [item[2] for item in ranked]

    def _resolve_attack(self, phase: str, territory_id: int, attacker_id: int, defender_id: int, result: dict) -> list[dict]:
        effects: list[dict] = []
        winner_id = result["winner_id"]
        cells = cells_by_id(self.cells)
        target = cells[territory_id]
        attacker_entry = result["answers"].get(str(attacker_id), {})
        defender_entry = result["answers"].get(str(defender_id), {})

        attacker_wins = False
        if result["question"].question_type == QuestionType.ESTIMATE:
            attacker_wins = winner_id == attacker_id
        else:
            if attacker_entry.get("is_correct") and not defender_entry.get("is_correct"):
                attacker_wins = True
            elif attacker_entry.get("is_correct") and defender_entry.get("is_correct"):
                attacker_wins = winner_id == attacker_id

        if phase == MatchState.BASE_SIEGE:
            if attacker_wins:
                same_target = self.siege_state["territory_id"] == territory_id and self.siege_state["attacker_id"] == attacker_id
                self.siege_state["territory_id"] = territory_id
                self.siege_state["attacker_id"] = attacker_id
                self.siege_state["streak"] = self.siege_state["streak"] + 1 if same_target else 1
                previous_hp = target["hp"]
                target["hp"] = max(0, previous_hp - 1)
                effects.append(
                    {
                        "type": "base_siege_progress",
                        "territory_id": territory_id,
                        "attacker_id": attacker_id,
                        "defender_id": defender_id,
                        "streak": self.siege_state["streak"],
                        "previous_hp": previous_hp,
                        "new_hp": target["hp"],
                    }
                )
                if target["hp"] <= 0:
                    previous_owner_id = target["owner_id"]
                    target["owner_id"] = attacker_id
                    self.siege_state = {"territory_id": None, "attacker_id": None, "streak": 0}
                    effects.append(
                        {
                            "type": "base_captured",
                            "territory_id": territory_id,
                            "previous_owner_id": previous_owner_id,
                            "new_owner_id": attacker_id,
                        }
                    )
            else:
                self.siege_state = {"territory_id": None, "attacker_id": None, "streak": 0}
                effects.append(
                    {
                        "type": "base_siege_held",
                        "territory_id": territory_id,
                        "defender_id": defender_id,
                        "remaining_hp": target["hp"],
                    }
                )
        elif attacker_wins:
            previous_owner_id = target["owner_id"]
            target["owner_id"] = attacker_id
            target["hp"] = 1
            self.map_data["stats"][str(attacker_id)]["territories_won"] += 1
            effects.append(
                {
                    "type": "territory_captured",
                    "territory_id": territory_id,
                    "previous_owner_id": previous_owner_id,
                    "new_owner_id": attacker_id,
                }
            )
        else:
            effects.append(
                {
                    "type": "territory_held",
                    "territory_id": territory_id,
                    "defender_id": defender_id,
                }
            )
        return effects

    def _valid_targets_for_player(self, player_id: int, mode: str) -> list[int]:
        if mode == "expansion":
            adjacent_targets = owned_neighbor_targets(self.cells, self.adjacency, player_id, target_mode="neutral")
            if adjacent_targets:
                return adjacent_targets
            return [cell["territory_id"] for cell in self.cells if cell["owner_id"] is None]
        if mode == "battle":
            return owned_neighbor_targets(self.cells, self.adjacency, player_id, target_mode="enemy")
        return []

    def _neutral_cells_exist(self) -> bool:
        return any(cell["owner_id"] is None for cell in self.cells)

    def _dominant_owner_id(self) -> int | None:
        owners = {cell["owner_id"] for cell in self.cells if cell["owner_id"] is not None}
        return next(iter(owners)) if len(owners) == 1 else None

    def _is_game_over(self) -> bool:
        if self._base_captured():
            return True
        dominant_owner = self._dominant_owner_id()
        if dominant_owner is not None and all(cell["owner_id"] is not None for cell in self.cells):
            return True
        return False

    def _base_captured(self) -> bool:
        for player_id in self.player_ids:
            base = find_player_base(self.cells, player_id)
            if base is not None and base["owner_id"] != player_id:
                return True
        return False

    def _finish_match(self, reason: str) -> None:
        result_view = self._build_result_view(reason)
        leaderboard = result_view["leaderboard"]
        result_data = {
            "reason": reason,
            "leaderboard": leaderboard,
            "result_view": result_view,
            "finished_at": timezone.now().isoformat(),
        }
        self.map_data["current_question"] = None
        self.map_data["question_window"] = None
        self.map_data["current_selection_prompt"] = None
        self.map_data["pause_window"] = None
        self.map_data["pause_message"] = None
        self.map_data["leaderboard"] = leaderboard
        self._persist_map_data(current_state=MatchState.FINISHED, result_data=result_data)
        self._publish(
            {
                "type": "match.finished",
                "match_id": self.match_id,
                "reason": reason,
                "current_state": MatchState.FINISHED,
                "map_data": self.map_data,
                "leaderboard": leaderboard,
                "result_view": result_view,
                "player_ids": self.player_ids,
            }
        )
        self._reset_lobby_state()

    def _captured_base_entry(self) -> dict | None:
        for cell in self.cells:
            if cell.get("is_base") and cell.get("owner_id") is not None and cell.get("owner_id") != cell.get("base_owner_id"):
                return cell
        return None

    def _build_result_entry(self, player_id: int) -> dict:
        counts = territory_counts(self.cells)
        stats = self.map_data["stats"][str(player_id)]
        base = find_player_base(self.cells, player_id)
        base_hp = base["hp"] if base is not None and base["owner_id"] == player_id else 0
        territories = counts.get(player_id, 0)
        average_response_ms = 0
        if stats["answers_submitted"]:
            average_response_ms = int(stats["total_response_ms"] / stats["answers_submitted"])
        return {
            "player_id": player_id,
            "territories": territories,
            "base_hp": base_hp,
            "correct_answers": stats["correct_answers"],
            "won_questions": stats.get("won_questions", 0),
            "average_response_ms": average_response_ms,
            "score": territories * 10 + base_hp * 25 + stats.get("won_questions", 0) * 4,
        }

    def _build_standard_leaderboard(self) -> list[dict]:
        leaderboard = [self._build_result_entry(player_id) for player_id in self.player_ids]
        leaderboard.sort(key=lambda item: (-item["score"], -item["territories"], -item["won_questions"], item["average_response_ms"], item["player_id"]))
        return leaderboard

    def _build_conquest_leaderboard(self, conqueror_id: int) -> list[dict]:
        leaderboard = [self._build_result_entry(player_id) for player_id in self.player_ids]
        leaderboard.sort(
            key=lambda item: (
                0 if item["player_id"] == conqueror_id else 1,
                -item["won_questions"],
                -item["territories"],
                -item["base_hp"],
                item["average_response_ms"],
                item["player_id"],
            )
        )
        return leaderboard

    def _build_round_limit_leaderboard(self) -> list[dict]:
        leaderboard = [self._build_result_entry(player_id) for player_id in self.player_ids]
        leaderboard.sort(key=lambda item: (-item["territories"], -item["base_hp"], -item["won_questions"], item["average_response_ms"], item["player_id"]))
        return leaderboard

    def _build_result_view(self, reason: str) -> dict:
        captured_base = self._captured_base_entry()
        if captured_base is not None:
            conqueror_id = captured_base["owner_id"]
            return {
                "mode": "conquest",
                "winner_id": conqueror_id,
                "captured_base_owner_id": captured_base["base_owner_id"],
                "leaderboard": self._build_conquest_leaderboard(conqueror_id),
            }
        if reason == "battle_round_limit":
            return {
                "mode": "round_limit",
                "tiebreak": "hexes > castle hp > won questions",
                "leaderboard": self._build_round_limit_leaderboard(),
            }
        return {
            "mode": "standard",
            "leaderboard": self._build_standard_leaderboard(),
        }

    def _reset_lobby_state(self) -> None:
        match = Match.objects.select_related("lobby", "lobby__host", "lobby__guest").get(public_id=self.match_id)
        if match.lobby is None:
            return
        lobby = match.lobby
        lobby.state = LobbyState.CLOSED
        lobby.save(update_fields=["state", "updated_at"])
        self._publish(
            {
                "type": "lobby.closed",
                "reason": "match_finished",
                "message": "The match ended and the lobby was closed.",
                "player_ids": [player_id for player_id in [lobby.host_id, lobby.guest_id] if player_id],
            },
            routing_key="player.lobby",
        )

    def _next_question(self) -> Question:
        if self.test_mode:
            question = self.test_questions[self.test_question_index % len(self.test_questions)]
            self.test_question_index += 1
            return question

        if not self.remote_question_queue:
            self._refill_remote_question_queue()

        question = self.remote_question_queue.pop(0)
        self._used_question_ids.add(question.id)
        return question

    def _refill_remote_question_queue(self) -> None:
        self._publish_preparing_questions()
        attempts = 0
        last_error = None
        while attempts < 5:
            try:
                batch = fetch_runtime_questions(
                    amount=100,
                    categories=self.settings_data.get("categories") or None,
                    question_types=self.settings_data.get("question_types") or None,
                    difficulties=self.settings_data.get("difficulties") or None,
                    batch_index=self.remote_batch_index,
                )
                self.remote_batch_index += 1
                fresh_questions = [question for question in batch if question.id not in self._used_question_ids]
                if fresh_questions:
                    self.remote_question_queue.extend(fresh_questions)
                    return
                if batch:
                    self.remote_question_queue.extend(batch)
                    return
            except Exception as exc:
                last_error = exc
            attempts += 1

        raise Question.DoesNotExist("No OpenTDB runtime questions available for this match.") from last_error

    def _publish_preparing_questions(self) -> None:
        current_state = self.map_data.get("current_state") or MatchState.QUEUED
        self.map_data["current_question"] = None
        self.map_data["question_window"] = None
        self.map_data["current_selection_prompt"] = None
        self.map_data["pause_window"] = None
        self.map_data["pause_message"] = "Preparing the questions..."
        self._persist_map_data(current_state=current_state)
        self._publish(
            {
                "type": "round.pause",
                "match_id": self.match_id,
                "round_number": self.round_number,
                "phase": current_state,
                "message": "Preparing the questions...",
                "timer_seconds": None,
                "started_at": None,
                "ends_at": None,
                "map_data": self.map_data,
                "player_ids": self.player_ids,
            }
        )

    @staticmethod
    def _response_ms(started_at, received_at: str) -> int:
        received_dt = parse_datetime(received_at) if received_at else None
        if received_dt is None:
            return 10**9
        return max(0, int((received_dt - started_at).total_seconds() * 1000))

    @staticmethod
    def _parse_float(value) -> float | None:
        if value in {None, ""}:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
