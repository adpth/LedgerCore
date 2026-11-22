# README review

Two fresh reviewers checked the full README in each of five rounds. One compared the instructions with the code and configuration. The other checked clarity, setup and how the README described verification. The fifth round reached the review cap. All actionable findings were addressed. The loop recorded fewer than two consecutive clean rounds.

| Round | Changes made |
| --- | --- |
| 1 | Documented the payout ID and provider payload, split runtime and migration credentials, and corrected the deployment permissions and demo examples. Confirmed the rendered reconciliation job already included OTLP configuration. |
| 2 | Clarified which triggers enforce each invariant, documented JWT and deployment prerequisites, and added an explicit replay iteration threshold and low-rate smoke command. |
| 3 | Required a transfer token from the target ledger, documented Kafka topic setup, and tightened the k6 assertion to require a nonempty entry ID. |
| 4 | Documented API errors, CSV order and pagination, added explicit runner architecture and PostgreSQL schema permissions, and validated k6 account pairs. |
| 5 | Added the Compose readiness check and exact Terraform commands. Added and verified the RDS CA mount for the API, relay, payout and both jobs. |

## Verification

- Nine integration tests passed against local PostgreSQL, including concurrent replays, competing transfers, database journal guards, reversals, payout retries, reconciliation and JWT permissions.
- A local two-process k6 run completed 50,000 originals and 50,000 retries with zero failed HTTP requests. PostgreSQL contained 50,000 distinct response IDs, 50,000 load-transfer entries and 50,000 associated outbox rows. Reconciliation found zero mismatches.
- The final k6 script sustained 100 transfers per second for 30 seconds with 6.29 ms p95 HTTP duration, zero dropped iterations and zero failed requests.
- A local Kafka broker acknowledged six seeded events, and a consumer received six distinct event IDs.
- The running demo completed one simulated payout and declined another. Both produced the expected closing journal entry, published their events and reconciled.
- Chromium exercised demo sign-in, journal details, filtering, CSV download, balances and reconciliation. The 390-pixel viewport contained the layout without page-wide overflow.
- Terraform validation, Helm lint and rendering, Compose configuration validation, and the OpenTelemetry collector and Prometheus metric checks passed.
