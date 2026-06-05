"""OTS → CTS conversion.

Turns an Open Team Sheet (species/ability/item/moves/nature, no EVs) into
a Closed Team Sheet (EVs assigned) so it can be loaded into a battle
simulator. EVs are sourced first from the guild's existing team bank
(matching by species, then ability → item → moves, weighted by how much
of the team's composition overlaps the OTS), then from an LLM-guessed
spread for any Pokemon with no bank match.

The package is split so each piece is independently testable:
  - `ev_model`   — format → EV regime (Champions vs legacy caps).
  - `bank`       — load + parse candidate teams from the sheet.
  - `ev_matcher` — pure scoring: pick the best bank spread for one mon.
  - `llm_guess`  — the Claude fallback for unmatched mons.
  - `converter`  — orchestrates the above into a finished `TeamData`.
"""
