# Back-test: do past events' real sponsors rank above non-sponsors?

**Cannot verify.** only 0 of 110 real sponsors had sponsored anything earlier in the data (need 10): too few repeat sponsors to test; only 0% of sponsor/non-sponsor pairs were decided (need 20%): most companies have no sponsorship history before the events.

- Events found: 19, tested: 9, sponsors: 110
- Real sponsors with an earlier sponsorship in the data: 0 (0%)
- Pairs decided (not tied): 0%
- AUC: 50% · mean recall in top 10: 20%
- Only `past_sponsorships` can be known at a past date, so this tests that signal, not the weights.
- Non-sponsors are companies that sponsor other events at or after the cutoff, so this compares repeat sponsors with later sponsors, not with companies that never sponsor.

| Event | Cutoff | Sponsors | With history | Non-sponsors | With history | AUC | Recall@k |
|---|---|---|---|---|---|---|---|
| TransTech India 2025 | 2025-12-31 | 26 | 0 | 86 | 0 | 50% | 9% |
| HealthCare 360 Summit | 2025-12-05 | 8 | 0 | 112 | 0 | 50% | 8% |
| AI Nexus 2026: The India - New Zealand AI Summit | 2026-01-31 | 3 | 0 | 80 | 0 | 50% | 12% |
| SulGas Conference 2026 | 2026-02-28 | 4 | 0 | 75 | 0 | 50% | 13% |
| BCIC Manufacturing Summit & Manufacturing Excellence Awards 2026 | 2026-05-22 | 11 | 0 | 49 | 0 | 50% | 17% |
| 19th Annual Conference on Solar Power in India | 2026-05-31 | 6 | 0 | 43 | 0 | 50% | 20% |
| Sustainable Agriculture Summit & Awards | 2026-05-14 | 12 | 0 | 60 | 0 | 50% | 14% |
| Cypher 2026 | 2026-08-31 | 26 | 0 | 16 | 0 | 50% | 24% |
| 5th Edition of Smart Meter Summit 2026 | 2026-09-17 | 14 | 0 | 1 | 0 | 50% | 67% |
