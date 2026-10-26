# House rules: the house rules (.aid/)

- These files are read by the agent while it reviews its own merge requests, and
  this repository is Python: a rule names Python constructs and real modules
  under `reviewer/`, `collector/` or `tests/`, never Go ones.
- `.aid/` is published with the code. A rule naming an internal hostname,
  project or credential is a defect.
