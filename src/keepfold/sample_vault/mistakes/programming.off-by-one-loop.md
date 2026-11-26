---
id: "programming.off-by-one-loop"
title: "Off-by-one at the end of a loop"
concepts: ["[[loops]]"]
sample: true
---
# Off-by-one at the end of a loop

## Pattern
Writes `range(1, n)` when the last number `n` should be included.

## Fix
Say the last value out loud, then check it against the range end.

## Seen
FizzBuzz, first attempt.
