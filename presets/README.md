# Preset recovery policies (text injected into the agent's system prompt)
| File | Role | Provenance |
|---|---|---|
| `positive_control_blind_retry.txt` | **Positive control**: deliberately unsafe blind retry. Used only to prove the harness detects duplicates; never a candidate. Runs only against the synthetic isolated site. | experiment 06 pilot, arm B |
| `generic_guard.txt` | Candidate: generic completion-verification guard | experiment 06 Phase 3 / pilot, arm G |
| `cautious_no_resubmit.txt` | Candidate: verify by in-site navigation, bounded re-checks, never resubmit, abstain if unconfirmed | experiment 06 pilot, arm C |

The experiment-06 analyst policy (arm T) is not a preset: AI-generated diagnosis was retired as a differentiator.
