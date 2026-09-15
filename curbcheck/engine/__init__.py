"""Legality and cost evaluation: pure functions over a rule stack and a time window.

`window` expands [T1, T2] into sub-intervals and answers "is this rule in force
now"; `resolve` collapses a stack into a verdict for one sub-interval; `cost`
prices walking, meters, and risk; `search` runs the radius query and ranks.
Nothing here opens the network and only `search` touches the database.
"""
