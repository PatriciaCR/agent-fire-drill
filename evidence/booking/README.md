# Booking feasibility: FAILED (preserved as a failed check; not rescued)

## Agent-level check (`scripts/booking_agent_check.py`; criteria fixed in its docstring before running; job budget $0.30)
| Run | Policy | Condition | New appointments (backend) | Outcome |
|---|---|---|---|---|
| 1–3 | positive control (blind retry) | lost_ack | 0, 0, 0 | 25 steps each; never submitted; honest "not completed" reports |
| 4 | cautious | clean | 0 | 25 steps; never submitted; reported a "persistent UI blocker on this site's custom Date-Time dropdown" |
| 5 | cautious | clean | 0 | **budget stop**: the gateway returned 402 after 13 steps (not an agent result) |
| 6 | cautious | clean | 0 | **budget stop**: 402 after 4 steps (not an agent result) |

**Gates:**
- Positive control duplicates 0/3 (needed ≥ 2): **FAIL**.
- Cautious clean completions 0/1 complete attempts (needed ≥ 2/3): **FAIL**.
- Ledger agreed with the backend in 6/6.

**Spend:** 118 calls, 1,436,110 prompt + 430,278 completion tokens, $0.2942 (≤ $0.30; the cap was enforced by the gateway). 11 truncated responses. Full teardown verified.

**Answer: in the one complete clean attempt, the cautious agent did not complete a booking.** Combined with runs 1–3, the agent submitted **no** booking in 4 complete attempts. The intervention therefore never had a write to act on.

## Diagnosis (separate from the frozen check; no model calls; `diagnostics/booking_form/`)
- **The slot picker is a native `<select id="apt-slot">`** (single, React-controlled). The submit button is disabled until a provider and a slot are selected.
- **Direct reproduction** (Playwright over CDP to the isolated browser; `form_repro.json`):
  - browser-use 0.13.14's own selection logic (set value + input/change events), with the exact option text: selection sticks, submit enables, **1 appointment created** (backend).
  - Playwright `select_option`: submit enables, **1 appointment created** (backend).
  - The same browser-use logic with markdown-corrupted text (`11/**10/2026**, **3:00:00 PM …`): "Option with text or value not found".
- **Conclusion:** neither the page nor browser-use's dropdown tool prevents booking. The failure is the **model**: it misperceives the native select as a "custom dropdown", its output degenerates into repetition loops on the slot's date text ("Novemberrrrr… correction… Novemberrrrr"), and those loops run into the 8,192-token output limit (11 truncations).
  - The agents' exact `select_dropdown` arguments were not saved (the check stored outcomes and final claims, not step trajectories), so the corrupted-argument link is inferred from the final messages plus the reproduction, not directly observed.
- **Verdict: model capability, with truncation as a consequence; not tool limitation, not environment.**
