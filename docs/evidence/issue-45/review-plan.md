# Independent implementation challenge

Freeze one exact candidate SHA. Independent Claude (Opus) may read code/contracts,
inspect Git and run offline deterministic tests. One initial call, at most 16
turns, then a bounded repair and at most 8-turn delta review if needed. One
summary-only continuation is allowed if a tool-turn cap leaves the verdict
unprinted. No recursive review, Codex request, live lane, adoption or PR190 writes.
Retain exact SHA, JSON result, verdict and finding dispositions. This reviews the
runtime seam and oracle validity; it does not certify RPGFlow product correctness.
