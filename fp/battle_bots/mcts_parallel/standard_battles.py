import logging
import random
from copy import deepcopy

import constants
from data import all_move_json
from fp.battle_bots.mcts_parallel.team_sampler import populate_pkmn_from_set
from fp.helpers import natures
from fp.battle import Pokemon, Battle
from data.pkmn_sets import (
    SmogonSets,
    PokemonSet,
    PredictedPokemonSet,
    PokemonMoveset,
    MOVES_STRING,
    TeamDatasets,
)

logger = logging.getLogger(__name__)


TRICKABLE_ITEMS = {
    "choicespecs",
    "choicescarf",
    "choiceband",
    "assaultvest",
    "blacksludge",
    "stickybarb",
    "flameorb",
    "toxicorb",
}


def physical_boosting_move(mv: str, predicted_pkmn_set: PredictedPokemonSet) -> bool:
    if predicted_pkmn_set.pkmn_set.item in constants.CHOICE_ITEMS:
        return False

    # do not allow more than 1 non-physical move, excluding the boosting move
    if (
        sum(
            m != mv and all_move_json[m][constants.CATEGORY] != constants.PHYSICAL
            for m in predicted_pkmn_set.pkmn_moveset.moves
        )
        > 1
    ):
        return False

    return True


def special_boosting_move(mv: str, predicted_pkmn_set: PredictedPokemonSet) -> bool:
    if predicted_pkmn_set.pkmn_set.item in constants.CHOICE_ITEMS:
        return False

    # do not allow more than 1 non-special move, excluding the boosting move
    if (
        sum(
            m != mv and all_move_json[m][constants.CATEGORY] != constants.SPECIAL
            for m in predicted_pkmn_set.pkmn_moveset.moves
        )
        > 1
    ):
        return False

    return True


def choice_item(predicted_pkmn_set: PredictedPokemonSet):
    item = predicted_pkmn_set.pkmn_set.item
    match item:
        case "choiceband":
            logical_moves = [constants.PHYSICAL]
        case "choicespecs":
            logical_moves = [constants.SPECIAL]
        case "choicescarf":
            logical_moves = [constants.PHYSICAL, constants.SPECIAL]
        case _:
            raise ValueError("Invalid choice item: {}".format(item))

    num_illogical_moves = 0
    for mv in predicted_pkmn_set.pkmn_moveset.moves:
        if all_move_json[mv][constants.CATEGORY] not in logical_moves and mv not in [
            "trick",
            "switcheroo",
        ]:
            num_illogical_moves += 1

    return num_illogical_moves <= 1


def smogon_set_makes_sense(predicted_pkmn_set: PredictedPokemonSet):
    match predicted_pkmn_set.pkmn_set.item:
        case "toxicorb":
            if predicted_pkmn_set.pkmn_set.ability not in [
                "poisonheal",
                "quickfeet",
                "magicguard",
                "marvelscale",
                "guts",
                "toxicboost",
            ]:
                return False

        case "flameorb":
            if predicted_pkmn_set.pkmn_set.ability not in [
                "quickfeet",
                "magicguard",
                "guts",
                "flareboost",
            ]:
                return False

        case "choiceband" | "choicespecs" | "choicescarf":
            if not choice_item(predicted_pkmn_set):
                return False

        case "assaultvest":
            if predicted_pkmn_set.pkmn_set.ability != "klutz" and any(
                all_move_json[mv][constants.CATEGORY] == constants.STATUS
                for mv in predicted_pkmn_set.pkmn_moveset.moves
            ):
                return False

    match predicted_pkmn_set.pkmn_set.ability:
        case "poisonheal":
            if predicted_pkmn_set.pkmn_set.item != "toxicorb":
                return False

    for mv in predicted_pkmn_set.pkmn_moveset.moves:
        match mv:
            case "protect":
                if predicted_pkmn_set.pkmn_set.item in constants.CHOICE_ITEMS:
                    return False

            case (
                "swordsdance"
                | "dragondance"
                | "tidyup"
                | "sharpen"
                | "meditate"
                | "honeclaws"
                | "bellydrum"
                | "howl"
                | "shiftgear"
            ):
                if not physical_boosting_move(mv, predicted_pkmn_set):
                    return False

            case "nastyplot" | "tailglow":
                if not special_boosting_move(mv, predicted_pkmn_set):
                    return False

            case "bulkup" | "curse":
                if predicted_pkmn_set.pkmn_set.item in constants.CHOICE_ITEMS:
                    return False
                if predicted_pkmn_set.pkmn_set.evs[3] > 0:
                    return False
                if (
                    natures[predicted_pkmn_set.pkmn_set.nature]["plus"]
                    == constants.SPECIAL_ATTACK
                ):
                    return False

            case "calmmind":
                if predicted_pkmn_set.pkmn_set.item in constants.CHOICE_ITEMS:
                    return False
                if predicted_pkmn_set.pkmn_set.evs[1] > 0:
                    return False
                if (
                    natures[predicted_pkmn_set.pkmn_set.nature]["plus"]
                    == constants.ATTACK
                ):
                    return False

            case "trick" | "switcheroo":
                if predicted_pkmn_set.pkmn_set.item not in TRICKABLE_ITEMS:
                    return False

    return True


def adjust_probabilities_for_sampling(move_rates, num_moves=4):
    adjusted_rates = []

    for move, rate in move_rates:
        # Compute the adjusted rate for sampling
        adjusted_rate = 1 - (1 - rate) ** (1 / num_moves)
        adjusted_rates.append((move, adjusted_rate))

    return adjusted_rates


def get_filtered_sets(
    pkmn: Pokemon, remaining_sets: list[PokemonSet]
) -> list[PokemonSet]:
    # hidden power type isn't revealed so if the pokemon used hiddenpower it should
    # be replaced by the most likely hiddenpower that is still possible
    if pkmn.get_move(constants.HIDDEN_POWER) is not None:
        hidden_power_possibilities = [
            f"{constants.HIDDEN_POWER}{p}{constants.HIDDEN_POWER_ACTIVE_MOVE_BASE_DAMAGE_STRING}"
            for p in pkmn.hidden_power_possibilities
        ]
        for mv, _count in SmogonSets.get_raw_pkmn_sets_from_pkmn_name(
            pkmn.name, pkmn.base_name
        )[MOVES_STRING]:
            if mv in hidden_power_possibilities:
                pkmn.remove_move("hiddenpower")
                pkmn.add_move(mv)
                break

    filtered_sets = []
    for pkmn_set in remaining_sets:
        if smogon_set_makes_sense(
            PredictedPokemonSet(
                pkmn_set=pkmn_set,
                pkmn_moveset=PokemonMoveset(moves=tuple(m.name for m in pkmn.moves)),
            )
        ):
            filtered_sets.append(pkmn_set)

    return filtered_sets


def sample_pokemon_moveset_with_known_pkmn_set(pkmn: Pokemon, pkmn_set: PokemonSet):
    pkmn_known_moves = [m.name for m in pkmn.moves]
    num_known_moves = len(pkmn_known_moves)
    if num_known_moves >= 4:
        return pkmn_known_moves

    # 1: Use TeamDatasets' movesets to sample a moveset, if possible
    remaining_team_movesets = []
    for pkmn_moveset in TeamDatasets.get_all_possible_move_combinations(pkmn, pkmn_set):
        if not smogon_set_makes_sense(
            PredictedPokemonSet(
                pkmn_set=pkmn_set,
                pkmn_moveset=pkmn_moveset,
            )
        ):
            continue
        num_pkmn_moves = len(pkmn_moveset)

        # movesets with more moves known are more likely to be sampled
        if num_pkmn_moves == 2:
            count = pkmn_moveset.count
        elif num_pkmn_moves == 3:
            count = pkmn_moveset.count * 2
        else:
            count = pkmn_moveset.count * 3
        remaining_team_movesets.append((pkmn_moveset, count))

    if remaining_team_movesets:
        sampled_moveset, count = random.choices(
            remaining_team_movesets, weights=[m[1] for m in remaining_team_movesets]
        )[0]
        for mv in sampled_moveset:
            if mv not in pkmn_known_moves:
                pkmn_known_moves.append(mv)

    # If a full moveset was acquired from #1, we don't need to sample smogon moves
    if len(pkmn_known_moves) >= 4:
        return pkmn_known_moves

    # 2: Use SmogonSets to sample a moveset
    smogon_moves = [
        m
        for m in SmogonSets.get_raw_pkmn_sets_from_pkmn_name(pkmn.name, pkmn.base_name)[
            constants.MOVES
        ]
        if m[0] not in pkmn_known_moves
    ]
    moves_adjusted_probabilities = adjust_probabilities_for_sampling(
        smogon_moves, 4 - num_known_moves
    )
    index = 0
    while True:
        if len(pkmn_known_moves) >= 4 or not moves_adjusted_probabilities:
            break
        index = index % len(moves_adjusted_probabilities)
        mv, chance = moves_adjusted_probabilities[index]
        if random.random() < chance:
            pkmn_known_moves.append(mv)
            if not smogon_set_makes_sense(
                PredictedPokemonSet(
                    pkmn_set=pkmn_set,
                    pkmn_moveset=PokemonMoveset(moves=pkmn_known_moves),
                )
            ):
                pkmn_known_moves.pop()

            moves_adjusted_probabilities.pop(index)
        else:
            index += 1  # index is only incremented if the move is not added

    return pkmn_known_moves


def sample_pokemon(pkmn: Pokemon):
    # 1: TeamDatasets is not emptied and `get_all_remaining_sets` returned at least one set
    # Note: TeamDatasets are not sampled according to their counts
    # because the counts are not indicative of the actual distribution of sets
    # Also skip this step an amount of the time to get some variety
    remaining_team_sets = TeamDatasets.get_all_remaining_sets(pkmn)
    if remaining_team_sets and random.random() < 0.75:
        sampled_set = deepcopy(random.choice(remaining_team_sets))
        populate_pkmn_from_set(pkmn, sampled_set, source="teamdatasets-full")
        return

    # 2: TeamDatasets has at least 1 set in it that hasn't been invalidated,
    # but `get_all_remaining_sets` returned no sets because the accompanying movesets are invalid
    remaining_team_sets = [
        s
        for s in TeamDatasets.get_pkmn_sets_from_pkmn_name(pkmn.name, pkmn.base_name)
        if s.pkmn_set.set_makes_sense(pkmn) and smogon_set_makes_sense(s)
    ]
    if remaining_team_sets:
        sampled_set = deepcopy(random.choice(remaining_team_sets).pkmn_set)
        moves = sample_pokemon_moveset_with_known_pkmn_set(pkmn, sampled_set)
        sampled_set = PredictedPokemonSet(
            pkmn_set=sampled_set,
            pkmn_moveset=PokemonMoveset(moves=moves),
        )
        populate_pkmn_from_set(pkmn, sampled_set, source="teamdatasets-partial")
        return

    # 3: Try to sample from SmogonSets including moves
    # Sample a SmogonSet and then repeat the same process as in 2 to get a moveset
    remaining_smogon_sets = SmogonSets.get_all_remaining_sets(pkmn)
    remaining_smogon_sets = get_filtered_sets(pkmn, remaining_smogon_sets)
    if remaining_smogon_sets:
        sampled_smogon_set = deepcopy(
            random.choices(
                remaining_smogon_sets,
                weights=[s.count for s in remaining_smogon_sets],
            )[0]
        )
        moves = sample_pokemon_moveset_with_known_pkmn_set(pkmn, sampled_smogon_set)
        sampled_set = PredictedPokemonSet(
            pkmn_set=sampled_smogon_set,
            pkmn_moveset=PokemonMoveset(moves=moves),
        )
        populate_pkmn_from_set(pkmn, sampled_set, source="smogonsets")
        return

    logger.warning(f"Could not sample {pkmn.name}")


def prepare_battles(battle: Battle, num_battles: int) -> list[(Battle, float)]:
    sampled_battles = []
    for index in range(num_battles):
        logger.info("Sampling battle {}".format(index))
        battle_copy = deepcopy(battle)
        sample_pokemon(battle_copy.opponent.active)
        for pkmn in filter(lambda x: x.is_alive(), battle_copy.opponent.reserve):
            sample_pokemon(pkmn)

        battle_copy.opponent.lock_moves()
        sampled_battles.append((battle_copy, 1 / num_battles))

    return sampled_battles
