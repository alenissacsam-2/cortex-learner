---
id: "recursion-needs-a-base-case"
title: "Recursion needs a base case"
kind: "note"
note_type: "permanent"
summary: "A recursive function stops only when some input returns without calling itself."
tags: ["python"]
related: ["[[recursion]]", "[[functions]]"]
sample: true
---
# Recursion needs a base case

Without a base case each call makes another call until Python raises `RecursionError`.
Write the base case first, then make every other call move closer to it.

## Links
- [[recursion]]: the concept this note explains.
- [[functions]]: a recursive function is still an ordinary function.
