# Expense approval demo (second application for the reuse proof)
An independent, dependency-free Node.js app with synthetic in-memory data, written to test whether Agent Fire Drill's
fault injection, scoring and qualification engine transfer to an application they were not built for. It has no Agent
Fire Drill hooks. Everything Agent Fire Drill needs to know about it is in `contracts/expense-demo.json`.
The submit endpoint is deliberately not idempotent: every accepted submission issues a reimbursement.
